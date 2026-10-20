"""The application and the restore runbook as the role they really run as: a schema owner that is NOT a superuser.

The test servers connect as ``postgres`` (a superuser), which would hide a missing privilege. These tests create a
plain role that owns one schema, exactly what ``scripts/postgres-bootstrap.sql`` creates for the managed identity, and
run the whole application as it with ``ORQENTIS_POSTGRES_SET_ROLE``: migrations, the incident flow, the audit chain, the
lease and the anchoring job. The restore runbook (docs/operations/restore.md) is then rehearsed step by step against it,
with the operator acting as that role the way the Entra administrator group does after the bootstrap grant.
"""

from __future__ import annotations

import io
import secrets
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import asyncpg
import pytest
from rich.console import Console

from orqentis.bootstrap import build_container, open_postgres_session
from orqentis.config.clock import SystemClock
from orqentis.db_commands import run_audit_postgres, run_db
from orqentis.observability.anchors import AuditAnchorTask
from orqentis.storage.blob import BlobStorage
from orqentis.storage.postgres import PostgresAuditTrail
from tests.conftest import detect_and_investigate
from tests.support.audit_tamper import cut_back_to
from tests.support.postgres_app import AppStorage, azurite_blob_storage


def console() -> tuple[Console, io.StringIO]:
    buffer = io.StringIO()
    return Console(file=buffer, width=240, highlight=False), buffer


@pytest.fixture
async def owner(app_storage: AppStorage) -> AsyncIterator[tuple[AppStorage, str]]:
    """A schema owned by a role that has no superuser, createdb, createrole or replication rights."""
    token = secrets.token_hex(4)
    role, schema = f"id-test-{token}", f"lp_{token}"
    server = app_storage.server

    async def connect() -> asyncpg.Connection:
        return await asyncpg.connect(host=server.host, port=server.port, user=server.user, database=server.database)

    conn = await connect()
    try:
        await conn.execute(f'CREATE ROLE "{role}" NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOLOGIN')
        await conn.execute(f'CREATE SCHEMA "{schema}" AUTHORIZATION "{role}"')
    finally:
        await conn.close()
    try:
        yield replace(app_storage, schema=schema), role
    finally:
        conn = await connect()
        try:
            await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            await conn.execute(f'DROP OWNED BY "{role}"')
            await conn.execute(f'DROP ROLE IF EXISTS "{role}"')
        finally:
            await conn.close()


async def test_the_whole_application_runs_as_a_non_superuser_schema_owner(
    owner: tuple[AppStorage, str], tmp_path: Path
) -> None:
    storage, role = owner
    settings = storage.settings(tmp_path / "a", postgres_set_role=role)
    session = await open_postgres_session(settings, SystemClock(), migrate=True)  # migrations as the schema owner
    try:
        row = await session.storage.database.fetchrow(
            "SELECT current_user AS who, rolsuper, rolcreaterole, rolcreatedb "
            "FROM pg_roles WHERE rolname = current_user"
        )
        assert row is not None and row["who"] == role
        assert not (row["rolsuper"] or row["rolcreaterole"] or row["rolcreatedb"])  # the privileges it really has
    finally:
        await session.aclose()

    container = await build_container(settings)
    try:
        incident = await detect_and_investigate(container)  # events, incidents, jobs, approvals, evidence, audit
        assert incident.approval_ids
        lease = await container.lease.acquire("lp-test", timedelta(seconds=30))
        assert lease is not None  # the leader lease, with the database clock, as the schema owner
        anchors, epochs = container.audit._anchors, container.audit._epochs  # type: ignore[attr-defined]
        assert anchors is not None and epochs is not None
        assert await AuditAnchorTask(container.audit, anchors, container.clock, epochs=epochs).run() is not None  # type: ignore[arg-type]
        assert (await container.audit.verify()).valid
    finally:
        await container.investigation_jobs.shutdown()
        await container.aclose()
    out, text = console()
    assert await run_db(settings, "status", out) == 0 and "applied  0001_initial" in text.getvalue()


async def test_the_restore_runbook_rehearsed_end_to_end_as_the_roles_it_names(
    owner: tuple[AppStorage, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sections 6 and 7 of docs/operations/restore.md: verify, find the loss, open an epoch as an operator, resume."""
    storage, role = owner
    settings = storage.settings(tmp_path / "a", postgres_set_role=role)

    # A running environment with an anchored audit chain.
    container = await build_container(settings)
    try:
        await detect_and_investigate(container)
        anchors, epochs = container.audit._anchors, container.audit._epochs  # type: ignore[attr-defined]
        assert (await AuditAnchorTask(container.audit, anchors, container.clock, epochs=epochs).run()) is not None  # type: ignore[arg-type]
        before = await container.audit.head()
        assert before.sequence >= 4
    finally:
        await container.investigation_jobs.shutdown()
        await container.aclose()

    # "Restore to an earlier point": the last two audit records are gone (as a superuser, like PITR to a new server).
    restored_to = before.sequence - 2
    await cut_back_to(storage.server, storage.schema, restored_to)

    # Section 6 (`orqentis audit`, any identity): the loss is reported, not hidden.
    out, text = console()
    assert await run_audit_postgres(settings, out, start_epoch_reason=None) == 1
    assert "behind its latest anchor" in text.getvalue()

    # Section 7 with the application's identity (read-only on the epoch container): refused, and nothing changes.
    out, text = console()
    assert await run_audit_postgres(settings, out, start_epoch_reason="from the app") == 1
    assert "Audit epoch not started" in text.getvalue() and "restore.md" in text.getvalue()
    session = await open_postgres_session(settings, SystemClock(), migrate=False)
    try:
        assert (await session.storage.audit.head()).sequence == restored_to
        assert (await session.storage.audit.head()).epoch == 1
    finally:
        await session.aclose()

    # Section 7 with an operator's identity (Contributor on the epoch container).
    monkeypatch.setattr(BlobStorage, "open", staticmethod(azurite_blob_storage(storage.azurite, operator=True)))
    out, text = console()
    assert await run_audit_postgres(settings, out, start_epoch_reason="PITR restore of old to 07:00Z as new") == 0
    assert "Started audit epoch" in text.getvalue()
    monkeypatch.setattr(BlobStorage, "open", staticmethod(azurite_blob_storage(storage.azurite)))  # back to the app

    out, text = console()
    assert await run_audit_postgres(settings, out, start_epoch_reason=None) == 0  # "valid again"
    assert "'valid': True" in text.getvalue()

    # The application serves again on the restored database and anchors in the new epoch.
    container = await build_container(settings)
    try:
        audit = container.audit
        assert isinstance(audit, PostgresAuditTrail)
        await audit.record(actor="a", action="after-the-restore", outcome="OK")
        anchors, epochs = audit._anchors, audit._epochs
        assert anchors is not None and epochs is not None
        anchor = await AuditAnchorTask(audit, anchors, container.clock, epochs=epochs).run()
        assert anchor is not None and anchor.epoch == 2
        assert (await audit.verify()).valid
        assert [m.epoch for m in await epochs.all()] == [2]
        assert [r.epoch for r in await anchors.refs(1)] and [r.epoch for r in await anchors.refs(2)]
    finally:
        await container.investigation_jobs.shutdown()
        await container.aclose()
