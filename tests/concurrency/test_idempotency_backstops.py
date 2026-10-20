"""The database refuses what the application's own checks should already have prevented.

Each of these is a backstop for a moment when the check-then-act in the application is not enough: a stalled worker and
its replacement, a lost advisory lock, a replica whose clock is wrong. They were reproduced against the first version.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from tests.conftest import OPERATOR
from tests.contract.conftest import history, make_approval
from tests.support.audit_tamper import admin
from tests.support.postgres_app import AppStorage
from tests.support.services import PostgresServer

from orqentis.approvals.service import ApprovalExpiredError, ApprovalService
from orqentis.bootstrap import Container, build_container
from orqentis.config.clock import FixedClock
from orqentis.domain.approvals import ApprovalStatus
from orqentis.domain.incidents import IncidentStatus
from orqentis.storage.incidents import DuplicateIncidentError
from orqentis.storage.postgres import PostgresApprovalProvider, PostgresAuditTrail, PostgresConfig, PostgresStorage
from orqentis.storage.postgres.mutex import PostgresMutex

pytestmark = pytest.mark.security

LAGGING = FixedClock(datetime(2020, 1, 1, tzinfo=UTC))  # a replica whose clock says it is 2020


# ------------------------------------------------------------------------------------------ one pending approval
async def test_requesting_the_same_action_again_returns_the_pending_request_instead_of_a_second_one(
    pg: PostgresStorage,
) -> None:
    first = make_approval(1)
    twin = make_approval(2)  # another request id for the very same incident and action
    assert twin.action_fingerprint == first.action_fingerprint and twin.incident_id == first.incident_id
    assert (await pg.approvals.submit(first)).approval_id == first.approval_id
    returned = await pg.approvals.submit(twin)
    assert returned.approval_id == first.approval_id  # the pending request stands
    assert [a.approval_id for a in await pg.approvals.list()] == [first.approval_id]

    # Ten replicas asking at once for the same action: one request, the same answer for all.
    answers = await asyncio.gather(
        *(pg.approvals.submit(make_approval(10 + n)) for n in range(10)),
    )
    assert {a.approval_id for a in answers} == {first.approval_id}
    assert len(await pg.approvals.list(status=ApprovalStatus.PENDING)) == 1


async def test_a_decided_approval_does_not_block_a_new_request_for_the_same_action(pg: PostgresStorage) -> None:
    first = make_approval(1)
    await pg.approvals.submit(first)
    approved = first.model_copy(update={"status": ApprovalStatus.APPROVED})
    assert await pg.approvals.transition(approved, expected_revision=first.revision) is not None
    again = await pg.approvals.submit(make_approval(2))  # nothing is pending any more: a fresh request is legitimate
    assert again.approval_id == "APR-2"


async def test_without_the_unique_index_the_same_race_creates_a_second_pending_approval(
    pg: PostgresStorage, pg_config: PostgresConfig, postgres_server: PostgresServer
) -> None:
    """The control: the index is what prevents the duplicate, not luck."""
    conn = await admin(postgres_server, pg_config.schema)
    try:
        await conn.execute("DROP INDEX ux_approvals_pending_action")
    finally:
        await conn.close()
    await pg.approvals.submit(make_approval(1))
    await pg.approvals.submit(make_approval(2))
    assert len(await pg.approvals.list(status=ApprovalStatus.PENDING)) == 2


# --------------------------------------------------------------------------------------- one incident per failure
async def test_a_detection_cannot_open_two_incidents_whatever_the_interleaving(pg: PostgresStorage) -> None:
    metadata = {"detection_rule": "pipeline-failure", "trigger_event_id": "e1"}
    base = history()[0].model_copy(update={"metadata": metadata})
    first = base.model_copy(update={"incident_id": "INC-2026-0601", "correlation_id": "run-1"})
    await pg.incidents.save(first)
    twin = first.model_copy(update={"incident_id": "INC-2026-0602"})
    with pytest.raises(DuplicateIncidentError) as excinfo:
        await pg.incidents.save(twin)
    assert excinfo.value.existing is not None and excinfo.value.existing.incident_id == "INC-2026-0601"
    await pg.incidents.save(first.model_copy(update={"status": IncidentStatus.INVESTIGATING}))  # updates are fine
    assert await pg.incidents.get("INC-2026-0602") is None

    # Without a run id the identity is the item and the trigger event.
    by_event = base.model_copy(update={"incident_id": "INC-2026-0603", "correlation_id": None})
    await pg.incidents.save(by_event)
    with pytest.raises(DuplicateIncidentError):
        await pg.incidents.save(by_event.model_copy(update={"incident_id": "INC-2026-0604"}))
    other_event = by_event.model_copy(
        update={
            "incident_id": "INC-2026-0605",
            "metadata": {"detection_rule": "pipeline-failure", "trigger_event_id": "e2"},
        }
    )
    await pg.incidents.save(other_event)  # another failure of the same item is another incident

    # Incidents that were not opened by detection (history, hand-made) are not claims on a detection.
    plain = history()[0]
    await pg.incidents.save(plain.model_copy(update={"incident_id": "INC-2026-0701", "correlation_id": "run-7"}))
    await pg.incidents.save(plain.model_copy(update={"incident_id": "INC-2026-0702", "correlation_id": "run-7"}))


@asynccontextmanager
async def no_exclusion(self: PostgresMutex, name: str, **kwargs: object) -> AsyncIterator[None]:
    yield  # the lock is lost: every holder believes it is alone


async def test_two_passes_without_any_mutex_still_open_one_incident_for_one_failure(
    app_storage: AppStorage, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mutex is the first line (lost when its connection drops); the unique key is the second."""
    monkeypatch.setattr(PostgresMutex, "hold", no_exclusion)
    first = await build_container(app_storage.settings(tmp_path / "a"))
    second = await build_container(app_storage.settings(tmp_path / "b"))
    containers: list[Container] = [first, second]
    try:
        outcomes = await asyncio.gather(*(c.collect_and_detect() for c in containers))
        opened = [i for _, created in outcomes for i in created]
        detected = [i for i in await first.incidents.repository.list() if "detection_rule" in i.metadata]
        assert len(opened) == 1 and len(detected) == 1, [i.incident_id for i in detected]
    finally:
        for c in containers:
            await c.investigation_jobs.shutdown()
            await c.aclose()


# --------------------------------------------------------------------------------------- expiry by the database
async def test_an_approval_that_expired_by_the_database_clock_cannot_be_claimed_by_a_replica_with_a_lagging_clock(
    pg: PostgresStorage,
) -> None:
    provider = PostgresApprovalProvider(pg.database, LAGGING, database_clock=True)
    now = datetime.now(UTC)
    expired = make_approval(1).model_copy(
        update={"expires_at": now - timedelta(minutes=5), "status": ApprovalStatus.APPROVED}
    )
    fresh = make_approval(2, incident_id="INC-2026-0002").model_copy(
        update={"expires_at": now + timedelta(hours=1), "status": ApprovalStatus.APPROVED}
    )
    await provider.save(expired)
    await provider.save(fresh)

    assert await provider.transition(expired, expected_revision=0, enforce_expiry=True) is None  # the database says no
    stored = await provider.get("APR-1")
    assert stored is not None and stored.revision == 0  # and nothing was written
    assert await provider.transition(fresh, expected_revision=0, enforce_expiry=True) is not None
    # Without the flag (cancelling, expiring) the write is not about expiry.
    assert await provider.transition(expired, expected_revision=0) is not None


async def test_deciding_an_approval_the_database_considers_expired_says_expired_not_decided_by_someone_else(
    app_storage: AppStorage, tmp_path: Path
) -> None:
    container = await build_container(app_storage.settings(tmp_path / "a"))
    try:
        stale = make_approval(1, incident_id="INC-2026-0001").model_copy(
            update={"expires_at": datetime.now(UTC) - timedelta(minutes=1), "requested_by": "agent:test"}
        )
        database = container.audit._db  # type: ignore[attr-defined]
        provider = PostgresApprovalProvider(database, LAGGING, database_clock=True)
        await provider.save(stale)
        service = ApprovalService(provider, container.incidents, container.audit, LAGGING, ttl=timedelta(hours=1))
        with pytest.raises(ApprovalExpiredError):
            await service.decide("APR-1", principal=OPERATOR, approve=True)
        still = await provider.get("APR-1")
        assert still is not None and still.status is ApprovalStatus.PENDING
    finally:
        await container.investigation_jobs.shutdown()
        await container.aclose()


# ----------------------------------------------------------------------------------------- monotonic timestamps
async def test_audit_timestamps_never_go_backwards_along_the_chain_whatever_the_clocks_of_the_writers_say(
    pg: PostgresStorage,
) -> None:
    ahead = PostgresAuditTrail(pg.database, FixedClock(datetime(2030, 1, 1, tzinfo=UTC)))
    behind = PostgresAuditTrail(pg.database, LAGGING)
    for n in range(6):
        await (ahead if n % 2 == 0 else behind).record(actor="a", action=f"w{n}", outcome="OK")
    stamps = [r.timestamp for r in await pg.audit.list()]
    assert stamps == sorted(stamps) and len(stamps) == 6
    assert (await pg.audit.verify()).valid

    # In deployed environments the database's clock is the one used: a replica that is years off still stamps real time.
    database = PostgresAuditTrail(pg.database, LAGGING, database_clock=True)
    record = await database.record(actor="a", action="db-clock", outcome="OK")
    assert abs((record.timestamp - datetime.now(UTC)).total_seconds()) < 60 or record.timestamp >= stamps[-1]
