"""Versioned migrations: applied once, behind an advisory lock, and never edited after release."""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import replace

import asyncpg
import pytest

from orqentis.config.clock import SystemClock
from orqentis.storage.postgres import MigrationRunner, PostgresConfig, PostgresDatabase, PostgresStorage
from orqentis.storage.postgres.migrations import MigrationError, SchemaNotCurrentError, load_migrations
from tests.support.services import PostgresServer


def test_the_shipped_migrations_are_numbered_without_gaps_and_have_checksums() -> None:
    migrations = load_migrations()
    assert [m.version for m in migrations] == list(range(1, len(migrations) + 1)) and migrations
    assert all(len(m.checksum) == 64 and m.sql.strip() for m in migrations)


async def test_migrations_run_with_info_logging_enabled(
    pg_config: PostgresConfig, caplog: pytest.LogCaptureFixture
) -> None:
    """Reproduced: a reserved LogRecord key in the log call crashed the first migration once INFO was enabled."""
    db = await PostgresDatabase.connect(pg_config, None)
    try:
        with caplog.at_level("INFO", logger="orqentis.storage.postgres.migrations"):
            applied = await MigrationRunner(db).migrate()
        assert len(applied) == len(load_migrations())
        assert [r.getMessage() for r in caplog.records].count("migration applied") == len(applied)
    finally:
        await db.close()


async def test_migrate_applies_everything_once_and_records_checksums(pg_config: PostgresConfig) -> None:
    db = await PostgresDatabase.connect(pg_config, None)
    try:
        runner = MigrationRunner(db)
        before = await runner.status()
        assert not before.current and not before.applied and len(before.pending) == len(load_migrations())
        applied = await runner.migrate()
        assert [m.version for m in applied] == [m.version for m in load_migrations()]
        assert await runner.migrate() == []  # nothing left to do
        status = await runner.status()
        assert status.current and not status.pending and not status.unknown
        assert [(a.version, a.checksum) for a in status.applied] == [(m.version, m.checksum) for m in load_migrations()]
        await runner.ensure_current()
    finally:
        await db.close()


async def test_several_replicas_starting_together_migrate_exactly_once(pg_config: PostgresConfig) -> None:
    async def start() -> int:
        db = await PostgresDatabase.connect(pg_config, None)
        try:
            return len(await MigrationRunner(db).migrate())
        finally:
            await db.close()

    applied = await asyncio.gather(*(start() for _ in range(6)))
    assert sum(applied) == len(load_migrations())  # one replica did the work, the others found nothing to do
    db = await PostgresDatabase.connect(pg_config, None)
    try:
        rows = await db.fetch("SELECT version FROM schema_migrations ORDER BY version")
        assert [r["version"] for r in rows] == [m.version for m in load_migrations()]
    finally:
        await db.close()


async def test_an_app_that_may_not_migrate_refuses_to_start_on_an_old_schema(pg_config: PostgresConfig) -> None:
    with pytest.raises(SchemaNotCurrentError, match="orqentis db migrate"):
        await PostgresStorage.open(pg_config, None, SystemClock(), migrate=False)
    migrated = await PostgresStorage.open(pg_config, None, SystemClock(), migrate=True)
    await migrated.close()
    again = await PostgresStorage.open(pg_config, None, SystemClock(), migrate=False)  # current: starts
    await again.close()


async def test_an_edited_released_migration_is_detected(pg_config: PostgresConfig) -> None:
    db = await PostgresDatabase.connect(pg_config, None)
    try:
        await MigrationRunner(db).migrate()
        await db.execute("UPDATE schema_migrations SET checksum = $1 WHERE version = 1", "0" * 64)
        with pytest.raises(MigrationError, match="never be edited"):
            await MigrationRunner(db).status()
        with pytest.raises(MigrationError):
            await MigrationRunner(db).migrate()
    finally:
        await db.close()


async def test_a_schema_that_is_ahead_of_this_version_is_tolerated_during_a_rolling_deploy(
    pg_config: PostgresConfig,
) -> None:
    db = await PostgresDatabase.connect(pg_config, None)
    try:
        runner = MigrationRunner(db)
        await runner.migrate()
        await db.execute(
            "INSERT INTO schema_migrations (version, name, checksum) VALUES (99, 'from_the_future', $1)", "f" * 64
        )
        status = await runner.status()
        assert status.current and [u.version for u in status.unknown] == [99]
        await runner.ensure_current()  # the old revision keeps running against the newer schema
        assert await runner.migrate() == []
    finally:
        await db.close()


async def test_migrations_refuse_to_run_as_a_role_that_does_not_own_the_schema(
    pg_config: PostgresConfig, postgres_server: PostgresServer
) -> None:
    """Tables owned by an administrator would be invisible to the application's role."""
    role = f"owner_{secrets.token_hex(4)}"
    admin = await asyncpg.connect(
        host=postgres_server.host,
        port=postgres_server.port,
        user=postgres_server.user,
        database=postgres_server.database,
    )
    try:
        await admin.execute(f'CREATE ROLE "{role}"')
        await admin.execute(f'CREATE SCHEMA "{pg_config.schema}" AUTHORIZATION "{role}"')
        db = await PostgresDatabase.connect(pg_config, None)
        try:
            with pytest.raises(MigrationError, match="migrations must run as the schema owner"):
                await MigrationRunner(db).migrate()
            assert await db.fetchval("SELECT to_regclass('schema_migrations')") is None
        finally:
            await db.close()
    finally:
        await admin.execute(f'DROP SCHEMA IF EXISTS "{pg_config.schema}" CASCADE')
        await admin.execute(f'DROP ROLE IF EXISTS "{role}"')
        await admin.close()


async def test_a_failing_migration_leaves_nothing_behind(pg_config: PostgresConfig) -> None:
    db = await PostgresDatabase.connect(pg_config, None)
    try:
        good = load_migrations()
        broken = replace(good[0], version=len(good) + 1, name="broken", sql="CREATE TABLE half (id int); SELECT 1/0;")
        runner = MigrationRunner(db, [*good, broken])
        with pytest.raises(Exception, match="division by zero"):
            await runner.migrate()
        status = await MigrationRunner(db, [*good, broken]).status()
        assert [m.version for m in status.pending] == [broken.version]  # the good ones were applied, this one was not
        assert await db.fetchval("SELECT to_regclass('half')") is None  # and its first statement was rolled back
    finally:
        await db.close()
