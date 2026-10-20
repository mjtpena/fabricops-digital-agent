"""Port contract tests: every storage port runs the same suite against its local adapter and its cloud adapter.

The ``postgres`` and ``blob`` variants need PostgreSQL / Azurite (a server named in the environment, or Docker); without
them they are skipped, and in CI (``ORQENTIS_REQUIRE_SERVICES=1``) a missing service fails the run.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from azure.storage.blob.aio import ContainerClient
from tests.conftest import REPO

from orqentis.approvals.provider import ApprovalProvider, LocalApprovalProvider
from orqentis.config.clock import SystemClock
from orqentis.contracts.source import ContractsSource, FileContractsSource
from orqentis.domain.actions import Action, ActionType, RiskLevel
from orqentis.domain.approvals import ApprovalRequest
from orqentis.domain.events import EventType, OperationalEvent
from orqentis.domain.incidents import Incident
from orqentis.fabric.fixtures import load_history
from orqentis.observability.anchors import AuditAnchorStore, LocalAuditAnchorStore
from orqentis.observability.audit import AuditTrail, LocalAuditLog
from orqentis.observability.epochs import EpochMarkerStore, LocalEpochMarkerStore
from orqentis.runtime.job_store import InMemoryJobStore, JobStore
from orqentis.runtime.lease import InProcessLeaderLease, LeaderLease, LeaseTable
from orqentis.storage.blob import BlobContractsSource
from orqentis.storage.dependency_store import SqliteDependencyStore
from orqentis.storage.event_store import EventStore, LocalEventStore
from orqentis.storage.evidence_store import EvidenceStore, LocalEvidenceStore
from orqentis.storage.incidents import IncidentRepository, SqliteIncidentRepository
from orqentis.storage.postgres import PostgresStorage
from orqentis.storage.sqlite import SqliteDatabase
from orqentis.topology.graph import DependencyStore

T0 = datetime(2026, 10, 5, tzinfo=UTC)
LOCAL, CLOUD = "local", "cloud"


# ----------------------------------------------------------------------------------------------- the adapters
@pytest.fixture(params=[LOCAL, "postgres"])
def backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
def pg_storage(backend: str, request: pytest.FixtureRequest) -> PostgresStorage | None:
    return request.getfixturevalue("pg") if backend == "postgres" else None  # type: ignore[no-any-return]


@pytest.fixture
def event_store(pg_storage: PostgresStorage | None, tmp_path: Path) -> Iterator[EventStore]:
    if pg_storage is not None:
        yield pg_storage.events
        return
    db = SqliteDatabase(tmp_path / "events.sqlite3")
    yield LocalEventStore(db)
    db.close()


@pytest.fixture
def incident_repository(pg_storage: PostgresStorage | None, tmp_path: Path) -> Iterator[IncidentRepository]:
    if pg_storage is not None:
        yield pg_storage.incidents
        return
    db = SqliteDatabase(tmp_path / "state.sqlite3")
    yield SqliteIncidentRepository(db)
    db.close()


@pytest.fixture
def dependency_store(pg_storage: PostgresStorage | None, tmp_path: Path) -> Iterator[DependencyStore]:
    if pg_storage is not None:
        yield pg_storage.dependencies
        return
    db = SqliteDatabase(tmp_path / "state.sqlite3")
    yield SqliteDependencyStore(db)
    db.close()


@pytest.fixture
def approval_provider(pg_storage: PostgresStorage | None, tmp_path: Path) -> Iterator[ApprovalProvider]:
    if pg_storage is not None:
        yield pg_storage.approvals
        return
    db = SqliteDatabase(tmp_path / "state.sqlite3")
    yield LocalApprovalProvider(db)
    db.close()


@pytest.fixture
def audit_trail(pg_storage: PostgresStorage | None, tmp_path: Path) -> AuditTrail:
    if pg_storage is not None:
        return pg_storage.audit
    return LocalAuditLog(tmp_path / "audit" / "audit.jsonl", SystemClock())


class LeaseFactory:
    """Makes leases for named holders that share one lease table (in process) or one database."""

    def __init__(self, pg_storage: PostgresStorage | None) -> None:
        self._pg = pg_storage
        self._table = LeaseTable()

    def holder(self, holder_id: str) -> LeaderLease:
        if self._pg is not None:
            from orqentis.storage.postgres import PostgresLeaderLease

            return PostgresLeaderLease(self._pg.database, SystemClock(), holder_id=holder_id)
        return InProcessLeaderLease(SystemClock(), holder_id=holder_id, table=self._table)


@pytest.fixture
def leases(pg_storage: PostgresStorage | None) -> LeaseFactory:
    return LeaseFactory(pg_storage)


@pytest.fixture
def job_store(pg_storage: PostgresStorage | None) -> JobStore:
    return pg_storage.jobs if pg_storage is not None else InMemoryJobStore(SystemClock())


@pytest.fixture(params=[LOCAL, "blob"])
def evidence_backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
def evidence_store(evidence_backend: str, request: pytest.FixtureRequest, tmp_path: Path) -> EvidenceStore:
    if evidence_backend == "blob":
        return request.getfixturevalue("blob_evidence")  # type: ignore[no-any-return]
    return LocalEvidenceStore(tmp_path / "evidence")


@pytest.fixture
def anchor_store(evidence_backend: str, request: pytest.FixtureRequest, tmp_path: Path) -> AuditAnchorStore:
    if evidence_backend == "blob":
        return request.getfixturevalue("blob_anchors")  # type: ignore[no-any-return]
    return LocalAuditAnchorStore(tmp_path / "anchors")


class ContractsPublisher:
    """What a person does to the contracts file (put a new version, take it away) and the source the app reads."""

    def __init__(
        self,
        source: ContractsSource,
        put: Callable[[bytes], Awaitable[None]],
        remove: Callable[[], Awaitable[None]],
    ) -> None:
        self.source = source
        self.put = put
        self.remove = remove


@pytest.fixture(params=[LOCAL, "blob"])
def contracts_backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
def contracts_publisher(contracts_backend: str, request: pytest.FixtureRequest, tmp_path: Path) -> ContractsPublisher:
    if contracts_backend == "blob":
        container: ContainerClient = request.getfixturevalue("blob_contracts_container")

        async def put_blob(data: bytes) -> None:
            await container.upload_blob("estate.json", data, overwrite=True)

        async def remove_blob() -> None:
            await container.delete_blob("estate.json")

        return ContractsPublisher(BlobContractsSource(container, "estate.json"), put_blob, remove_blob)

    path = tmp_path / "estate.json"

    async def put_file(data: bytes) -> None:
        path.write_bytes(data)

    async def remove_file() -> None:
        path.unlink()

    return ContractsPublisher(FileContractsSource(path), put_file, remove_file)


@pytest.fixture
def epoch_store(evidence_backend: str, request: pytest.FixtureRequest, tmp_path: Path) -> EpochMarkerStore:
    if evidence_backend == "blob":
        return request.getfixturevalue("blob_epochs")  # type: ignore[no-any-return]
    return LocalEpochMarkerStore(tmp_path / "epochs")


# -------------------------------------------------------------------------------------------- sample objects
def make_event(
    i: int, item: str = "a", kind: EventType = EventType.PIPELINE_RUN_SUCCEEDED, *, key: str | None = None
) -> OperationalEvent:
    return OperationalEvent(
        event_id=f"e{i}",
        event_type=kind,
        tenant_id="t",
        workspace_id="w1" if item == "a" else "w2",
        item_id=item,
        correlation_id="run-1" if i % 2 else None,
        timestamp=T0 + timedelta(minutes=i),
        summary="s",
        dedupe_key=key or f"k{i}",
        source="test",
    )


def history() -> list[Incident]:
    return load_history(REPO / "fixtures")


def make_approval(i: int = 1, incident_id: str = "INC-2026-0001", *, revision: int = 0) -> ApprovalRequest:
    action = Action(
        action_id=f"ACT-{i}",
        incident_id=incident_id,
        action_type=ActionType.RERUN_PIPELINE,
        title="Rerun",
        target_item_id="pipeline-1",
        requested_by="agent:test",
        created_at=T0,
    )
    return ApprovalRequest(
        approval_id=f"APR-{i}",
        incident_id=incident_id,
        proposed_action=action,
        action_fingerprint=action.fingerprint(),
        risk=RiskLevel.MEDIUM_RISK,
        reason="because",
        required_role="FabricOps.Operator",
        requested_by="agent:test",
        requested_at=T0 + timedelta(minutes=i),
        expires_at=T0 + timedelta(hours=4),
        revision=revision,
    )
