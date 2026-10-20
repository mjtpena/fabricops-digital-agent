"""Regression tests for the independent review of the storage work (WS6). Each was reproduced before it was fixed.

* A claim that commits just as the worker loop is cancelled belonged to no task and stayed ``RUNNING``.
* A worker that could not heartbeat for a TTL re-claimed, and ran a second time, a job its own task was still running.
* A job row this revision cannot read must not stay ``RUNNING`` with no task.
* A NUL character in an id taken from a URL was a database error (500) instead of "not found".
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import asyncpg
import httpx
import pytest
from tests.conftest import detect
from tests.support.polling import poll
from tests.support.postgres_app import AppStorage
from tests.support.reasoners import GatedReasoner
from tests.support.services import PostgresServer

from orqentis.api.app import create_app
from orqentis.bootstrap import build_container
from orqentis.domain.principals import SYSTEM_PRINCIPAL
from orqentis.runtime.job_store import InvestigationJob, InvestigationJobStatus, InvestigationTrigger
from orqentis.storage.postgres import PostgresConfig, PostgresStorage
from orqentis.storage.postgres.jobs import PostgresJobStore

VIEWER = {"X-Orqentis-User": "victor@contoso.com"}
# The smallest claim lifetime the settings allow (as in test_restart_and_deploy.py).
FAST_JOBS = {"investigation_claim_ttl_seconds": 10, "investigation_poll_interval_seconds": 1}


async def raw_jobs(app_storage: AppStorage) -> list[asyncpg.Record]:
    conn = await asyncpg.connect(
        host=app_storage.server.host,
        port=app_storage.server.port,
        user=app_storage.server.user,
        database=app_storage.server.database,
    )
    try:
        return list(
            await conn.fetch(
                f'SELECT job_id, status, attempts, claim_seq, claimed_by FROM "{app_storage.schema}".investigation_jobs'  # noqa: S608
            )
        )
    finally:
        await conn.close()


@pytest.mark.security
async def test_a_claim_that_commits_as_the_worker_loop_is_cancelled_is_handed_back_at_shutdown(
    app_storage: AppStorage, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    container = await build_container(app_storage.settings(tmp_path / "a", **FAST_JOBS))
    committed = asyncio.Event()
    real_claim = PostgresJobStore.claim_next

    async def claim_then_stall(self: PostgresJobStore, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        claimed = await real_claim(self, *args, **kwargs)  # type: ignore[arg-type]
        if claimed is not None:
            committed.set()
            await asyncio.Event().wait()  # the worker loop is cancelled here: the claim never reaches a task
        return claimed

    monkeypatch.setattr(PostgresJobStore, "claim_next", claim_then_stall)
    try:
        incident = await detect(container)
        await container.investigation_jobs.start()
        submission = await container.investigation_jobs.submit(
            incident.incident_id, requested_by=SYSTEM_PRINCIPAL, trigger=InvestigationTrigger.API
        )
        async with asyncio.timeout(30):
            await committed.wait()
        assert [r["status"] for r in await raw_jobs(app_storage)] == ["RUNNING"]  # claimed, and nobody is running it

        await container.investigation_jobs.shutdown()

        (row,) = await raw_jobs(app_storage)
        assert row["job_id"] == submission.job.job_id
        assert row["status"] == "QUEUED" and row["claimed_by"] is None  # handed back at once, not after the TTL
        assert row["attempts"] == 0  # and it did not cost the job an attempt
    finally:
        await container.aclose()


@pytest.mark.security
async def test_a_worker_does_not_take_back_and_rerun_a_job_its_own_task_is_still_running(
    app_storage: AppStorage, tmp_path: Path
) -> None:
    gate = GatedReasoner()
    container = await build_container(app_storage.settings(tmp_path / "a", **FAST_JOBS), reasoner=gate)
    try:
        incident = await detect(container)
        await container.investigation_jobs.start()
        submission = await container.investigation_jobs.submit(
            incident.incident_id, requested_by=SYSTEM_PRINCIPAL, trigger=InvestigationTrigger.API
        )
        async with asyncio.timeout(30):
            await gate.started.wait()
        # A database outage longer than the claim TTL: the claim looks expired while the task is alive.
        conn = await asyncpg.connect(
            host=app_storage.server.host,
            port=app_storage.server.port,
            user=app_storage.server.user,
            database=app_storage.server.database,
        )
        try:
            await conn.execute(
                f"UPDATE \"{app_storage.schema}\".investigation_jobs SET claim_expires_at = now() - interval '1 minute'"  # noqa: S608
            )
        finally:
            await conn.close()
        await asyncio.sleep(2.5)  # at least two polls of the worker loop

        (row,) = await raw_jobs(app_storage)
        assert (
            row["status"] == "RUNNING" and row["claim_seq"] == 1 and row["attempts"] == 0
        )  # not re-queued, not re-run

        gate.release.set()
        job = await poll(
            lambda: container.investigation_jobs.get(submission.job.job_id),
            lambda j: j is not None and not j.status.is_active,
            within=60,
            interval=0.2,
        )
        assert job is not None and job.status is InvestigationJobStatus.SUCCEEDED, job
        assert (await raw_jobs(app_storage))[0]["claim_seq"] == 1  # one claim, one run
    finally:
        gate.release.set()
        await container.investigation_jobs.shutdown()
        await container.aclose()


async def admin(server: PostgresServer, schema: str) -> asyncpg.Connection:
    conn = await asyncpg.connect(host=server.host, port=server.port, user=server.user, database=server.database)
    await conn.execute(f'SET search_path TO "{schema}"')
    return conn


async def test_a_job_row_this_revision_cannot_read_is_failed_for_free_instead_of_staying_running(
    pg: PostgresStorage, pg_config: PostgresConfig, postgres_server: PostgresServer
) -> None:
    """A newer revision wrote a field the strict model of this one does not know (a rolling deploy)."""
    job = InvestigationJob(
        job_id="INV-1",
        incident_id="INC-2026-0001",
        status=InvestigationJobStatus.QUEUED,
        trigger=InvestigationTrigger.API,
        requested_by=SYSTEM_PRINCIPAL.id,
        created_at=datetime(2026, 10, 20, 8, tzinfo=UTC),
    )
    await pg.jobs.enqueue(job, SYSTEM_PRINCIPAL)
    conn = await admin(postgres_server, pg_config.schema)
    try:
        await conn.execute('UPDATE investigation_jobs SET principal = \'{"id": "x", "from_the_future": 1}\'')
    finally:
        await conn.close()

    assert await pg.jobs.claim_next("w1", timedelta(seconds=30), datetime.now(UTC)) is None

    failed = await pg.jobs.get("INV-1")
    assert failed is not None and failed.status is InvestigationJobStatus.FAILED
    assert failed.error is not None and "could not be read" in failed.error
    assert not await pg.jobs.is_active("INC-2026-0001")  # the incident's slot is free again
    assert await pg.jobs.count_failed_attempts("INC-2026-0001") == 0  # and the failure is not the incident's fault


async def test_an_id_with_a_nul_character_is_simply_not_found(pg: PostgresStorage) -> None:
    for text in ("\x00", "INC-2026-0001\x00x", "a\x00"):
        assert await pg.incidents.get(text) is None
        assert await pg.approvals.get(text) is None
        assert await pg.jobs.get(text) is None
        assert await pg.jobs.latest_for(text) is None
        assert not await pg.jobs.is_active(text)
        assert await pg.jobs.count_failed_attempts(text) == 0
        assert await pg.audit.list(incident_id=text) == []


async def test_the_api_answers_404_for_an_id_with_a_nul_character_on_postgres(
    app_storage: AppStorage, tmp_path: Path
) -> None:
    container = await build_container(app_storage.settings(tmp_path / "a"))
    try:
        app = create_app(container.settings, container=container)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
            for path in (
                "/incidents/%00",
                "/investigations/%00",
                "/incidents/INC-2026-0001%00x/investigation",
                "/incidents/INC-2026-0001%00x",
            ):
                response = await http.get(path, headers=VIEWER)
                assert response.status_code == 404, (path, response.status_code, response.text)
    finally:
        await container.investigation_jobs.shutdown()
        await container.aclose()
