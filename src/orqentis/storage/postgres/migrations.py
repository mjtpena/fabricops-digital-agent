"""Versioned schema migrations: numbered SQL files and a version table, applied behind an advisory lock.

* ``migrations/NNNN_name.sql`` are applied in order, each in its own transaction together with the row in
  ``schema_migrations`` (version, name, SHA-256 of the file). Forward only: there are no down migrations.
* **Safe with several replicas.** The whole run holds one session-level advisory lock on a dedicated connection; a
  replica that starts while another is migrating waits, then finds nothing left to do. A migration that has been
  applied is never run again.
* **Edited migrations are detected.** The recorded checksum must match the file; a mismatch (somebody edited a released
  migration) stops the start-up instead of letting the code run against a schema it does not describe.
* **Deploys.** The new revision migrates while the old one is still serving, so a migration must stay compatible with
  the previous release (add before you use; remove only in a later release). A database that is *ahead* of this
  code (a newer revision already migrated it) is not an error.
* Run at start-up (``ORQENTIS_POSTGRES_MIGRATE_ON_START=true``, the default) or as a separate step with
  ``orqentis db migrate``; with migration at start-up off, the application refuses to start while migrations
  are pending.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from importlib import resources

import asyncpg

from orqentis.observability.logging import get_logger
from orqentis.storage.postgres.database import Connection, PostgresDatabase

_log = get_logger(__name__)

# Arbitrary, fixed 64-bit key of the migration advisory lock ("ORQM" in ASCII).
_MIGRATION_LOCK_KEY = 0x4F52514D
_FILE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")
_LOCK_WAIT_SECONDS = 180.0


class MigrationError(Exception):
    pass


class SchemaNotCurrentError(MigrationError):
    """Migrations are pending and automatic migration is off."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str
    checksum: str


@dataclass(frozen=True)
class AppliedMigration:
    version: int
    name: str
    checksum: str
    applied_at: datetime


@dataclass(frozen=True)
class MigrationStatus:
    applied: tuple[AppliedMigration, ...]
    pending: tuple[Migration, ...]
    unknown: tuple[AppliedMigration, ...]  # applied by a newer revision of the application

    @property
    def current(self) -> bool:
        return not self.pending


def load_migrations() -> list[Migration]:
    """The migrations shipped with this version of the application, in order."""
    found: list[Migration] = []
    for entry in resources.files("orqentis.storage.postgres").joinpath("migrations").iterdir():
        match = _FILE.match(entry.name)
        if not match:
            continue
        sql = entry.read_text(encoding="utf-8")
        found.append(Migration(int(match.group(1)), match.group(2), sql, hashlib.sha256(sql.encode()).hexdigest()))
    found.sort(key=lambda m: m.version)
    versions = [m.version for m in found]
    if versions != list(range(1, len(versions) + 1)):
        raise MigrationError(f"Migration files must be numbered 0001.. without gaps, found {versions}")
    return found


class MigrationRunner:
    def __init__(self, db: PostgresDatabase, migrations: list[Migration] | None = None) -> None:
        self._db = db
        self._migrations = migrations if migrations is not None else load_migrations()

    # ------------------------------------------------------------------------------------------ inspection
    async def status(self) -> MigrationStatus:
        applied = await self._applied()
        known = {m.version: m for m in self._migrations}
        for row in applied:
            migration = known.get(row.version)
            if migration is not None and migration.checksum != row.checksum:
                raise MigrationError(
                    f"Migration {row.version:04d}_{row.name} was applied with a different content than the file "
                    "this version ships: a released migration must never be edited"
                )
        done = {a.version for a in applied}
        return MigrationStatus(
            applied=tuple(a for a in applied if a.version in known),
            pending=tuple(m for m in self._migrations if m.version not in done),
            unknown=tuple(a for a in applied if a.version not in known),
        )

    async def _applied(self) -> list[AppliedMigration]:
        try:
            rows = await self._db.fetch(
                "SELECT version, name, checksum, applied_at FROM schema_migrations ORDER BY version"
            )
        except asyncpg.UndefinedTableError:
            return []
        return [AppliedMigration(r["version"], r["name"], r["checksum"], r["applied_at"]) for r in rows]

    async def ensure_current(self) -> None:
        """Raise unless every migration this version ships has been applied."""
        status = await self.status()
        if status.pending:
            names = ", ".join(f"{m.version:04d}_{m.name}" for m in status.pending)
            raise SchemaNotCurrentError(
                f"The database schema is behind this version of the application (pending: {names}). "
                "Run `orqentis db migrate`, or set ORQENTIS_POSTGRES_MIGRATE_ON_START=true."
            )
        if status.unknown:
            _log.warning(
                "database schema is ahead of this application version",
                extra={"newest_applied": status.unknown[-1].version},
            )

    # ---------------------------------------------------------------------------------------------- apply
    async def migrate(self) -> list[Migration]:
        """Apply pending migrations; returns the ones applied by this call (empty when another replica did it)."""
        applied_now: list[Migration] = []
        async with self._db.connection() as lock_conn:
            await self._lock(lock_conn)
            try:
                await self._ensure_schema_objects(lock_conn)
                status = await self.status()
                for migration in status.pending:
                    await self._apply(migration)
                    applied_now.append(migration)
                    _log.info("migration applied", extra={"version": migration.version, "name": migration.name})
            finally:
                await lock_conn.execute("SELECT pg_advisory_unlock($1)", _MIGRATION_LOCK_KEY)
        return applied_now

    async def _lock(self, conn: Connection) -> None:
        """Wait for the advisory lock (another replica may be migrating), up to a bounded time."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _LOCK_WAIT_SECONDS
        while True:
            if await conn.fetchval("SELECT pg_try_advisory_lock($1)", _MIGRATION_LOCK_KEY):
                return
            if loop.time() >= deadline:
                raise MigrationError("Timed out waiting for another process to finish migrating the database")
            await asyncio.sleep(0.5)

    async def _ensure_schema_objects(self, conn: Connection) -> None:
        schema = self._db.config.schema
        # Existence is checked first: CREATE SCHEMA IF NOT EXISTS still demands the CREATE privilege on the database,
        # which the application role does not have once the bootstrap step has created its schema.
        if not await conn.fetchval("SELECT 1 FROM pg_namespace WHERE nspname = $1", schema):
            await conn.execute(f'CREATE SCHEMA "{schema}"')
        owner = await conn.fetchval("SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = $1", schema)
        current = await conn.fetchval("SELECT current_user")
        if owner != current:
            # Tables made by another role would be invisible to the application (scripts/postgres-bootstrap.sql).
            raise MigrationError(
                f"The schema {schema!r} is owned by {owner!r} but this connection is {current!r}: migrations must "
                "run as the schema owner, which is the application's role (the application migrates when it starts)"
            )
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version integer PRIMARY KEY, name text NOT NULL, checksum text NOT NULL, "
            "applied_at timestamptz NOT NULL DEFAULT now())"
        )

    async def _apply(self, migration: Migration) -> None:
        async with self._db.transaction(fenced=False) as conn:
            await conn.execute(migration.sql)
            await conn.execute(
                "INSERT INTO schema_migrations (version, name, checksum) VALUES ($1, $2, $3)",
                migration.version,
                migration.name,
                migration.checksum,
            )
