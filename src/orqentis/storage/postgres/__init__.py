"""PostgreSQL adapters of the storage ports (ADR-0008, ADR-0017).

This package is the only place that imports the PostgreSQL driver. ``PostgresStorage.open`` connects once and builds
every adapter on the same pool; the composition root (``orqentis.bootstrap``) is its only caller besides the CLI.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Self

from orqentis.config.clock import Clock
from orqentis.observability.anchors import AuditAnchorStore
from orqentis.observability.epochs import EpochMarkerStore
from orqentis.storage.postgres.approvals import PostgresApprovalProvider
from orqentis.storage.postgres.audit import PostgresAuditTrail
from orqentis.storage.postgres.database import PasswordProvider, PostgresConfig, PostgresDatabase
from orqentis.storage.postgres.dependencies import PostgresDependencyStore
from orqentis.storage.postgres.event_store import PostgresEventStore
from orqentis.storage.postgres.incidents import PostgresIncidentRepository
from orqentis.storage.postgres.jobs import PostgresJobStore
from orqentis.storage.postgres.lease import PostgresLeaderLease
from orqentis.storage.postgres.migrations import MigrationRunner
from orqentis.storage.postgres.mutex import PostgresMutex

__all__ = [
    "MigrationRunner",
    "PasswordProvider",
    "PostgresApprovalProvider",
    "PostgresAuditTrail",
    "PostgresConfig",
    "PostgresDatabase",
    "PostgresDependencyStore",
    "PostgresEventStore",
    "PostgresIncidentRepository",
    "PostgresJobStore",
    "PostgresLeaderLease",
    "PostgresMutex",
    "PostgresStorage",
]


@dataclass
class PostgresStorage:
    """Every PostgreSQL adapter, sharing one connection pool."""

    database: PostgresDatabase
    events: PostgresEventStore
    incidents: PostgresIncidentRepository
    dependencies: PostgresDependencyStore
    approvals: PostgresApprovalProvider
    audit: PostgresAuditTrail
    lease: PostgresLeaderLease
    jobs: PostgresJobStore
    mutex: PostgresMutex

    @classmethod
    async def open(
        cls,
        config: PostgresConfig,
        password: PasswordProvider | None,
        clock: Clock,
        *,
        anchors: AuditAnchorStore | None = None,
        epochs: EpochMarkerStore | None = None,
        database_clock: bool = False,
        migrate: bool | None = True,
        holder_id: str | None = None,
    ) -> Self:
        """Connect, bring the schema up to date (True), insist that it already is (False) or leave it (None)."""
        db = await PostgresDatabase.connect(config, password)
        try:
            runner = MigrationRunner(db)
            if migrate:
                await runner.migrate()
            elif migrate is False:
                await runner.ensure_current()
            return cls(
                database=db,
                events=PostgresEventStore(db),
                incidents=PostgresIncidentRepository(db),
                dependencies=PostgresDependencyStore(db),
                approvals=PostgresApprovalProvider(db, clock, database_clock=database_clock),
                audit=PostgresAuditTrail(db, clock, anchors, epochs, database_clock=database_clock),
                lease=PostgresLeaderLease(db, clock, holder_id=holder_id),
                jobs=PostgresJobStore(db),
                mutex=PostgresMutex(db),
            )
        except BaseException:
            await db.close()
            raise

    async def close(self) -> None:
        await self.database.close()
