"""``orqentis db`` and the PostgreSQL parts of ``orqentis audit``: operator commands for the system of record.

They connect with the same Entra identity and settings as the application (a developer's ``az login`` through
``DefaultAzureCredential``, or a managed identity), never with a password.
"""

from __future__ import annotations

import getpass
import re
from typing import Any

from rich.console import Console

from orqentis.bootstrap import open_postgres_session
from orqentis.config.clock import SystemClock
from orqentis.config.settings import Settings, StorageBackend
from orqentis.observability.epochs import EpochMarkerAccessError
from orqentis.storage.postgres import MigrationRunner

_INCIDENT_ID = re.compile(r"^INC-(\d{4})-(\d+)$")


def _require_postgres(settings: Settings, console: Console) -> bool:
    if settings.storage_backend is StorageBackend.POSTGRES:
        return True
    console.print("[red]ORQENTIS_STORAGE_BACKEND must be postgres for this command.[/]")
    return False


async def run_db(settings: Settings, command: str, console: Console) -> int:
    if not _require_postgres(settings, console):
        return 2
    session = await open_postgres_session(settings, SystemClock(), migrate=None)
    try:
        runner = MigrationRunner(session.storage.database)
        if command == "migrate":
            applied = await runner.migrate()
            console.print(
                f"Applied {len(applied)} migration(s): " + ", ".join(f"{m.version:04d}_{m.name}" for m in applied)
                if applied
                else "The schema is up to date."
            )
            return 0
        if command == "status":
            status = await runner.status()
            for row in status.applied:
                console.print(f"applied  {row.version:04d}_{row.name}  {row.applied_at:%Y-%m-%d %H:%M:%S}Z")
            for migration in status.pending:
                console.print(f"[yellow]pending  {migration.version:04d}_{migration.name}[/]")
            for row in status.unknown:
                console.print(f"[cyan]newer    {row.version:04d}_{row.name} (applied by a newer version of the app)[/]")
            return 0 if status.current else 1
        if command == "reseed-counters":
            return await _reseed_counters(session, console)
        console.print(f"[red]Unknown db command {command!r}[/]")
        return 2
    finally:
        await session.aclose()


async def _reseed_counters(session: Any, console: Console) -> int:
    """Raise the incident counters above every incident that still has evidence in Blob storage.

    A point-in-time restore rewinds the database but not the evidence container; without this a new incident could be
    given the number of an old one whose evidence artefacts still exist.
    """
    highest: dict[int, int] = {}
    for incident_id in await session.blob.evidence.incident_ids():
        match = _INCIDENT_ID.match(incident_id)
        if match:
            year, number = int(match.group(1)), int(match.group(2))
            highest[year] = max(highest.get(year, 0), number)
    moved = await session.storage.incidents.raise_counters(highest)
    if moved:
        console.print("Raised incident counters: " + ", ".join(f"{y} -> {n}" for y, n in sorted(moved.items())))
    else:
        console.print("No incident counter was behind the stored evidence.")
    return 0


async def run_audit_postgres(settings: Settings, console: Console, *, start_epoch_reason: str | None) -> int:
    """Verify the audit chain (and its latest anchor); with a reason, start a new anchor epoch first."""
    session = await open_postgres_session(settings, SystemClock(), migrate=False)
    try:
        audit = session.storage.audit
        if start_epoch_reason is not None:
            try:
                record = await audit.start_epoch(actor=f"operator:{getpass.getuser()}", reason=start_epoch_reason)
            except EpochMarkerAccessError as exc:
                # Opening an epoch is an operator action the application's own identity cannot perform, on purpose.
                console.print(f"Audit epoch not started: {exc}. Nothing was changed. See docs/operations/restore.md.")
                return 1
            console.print(f"Started audit epoch (record {record.sequence}): {record.details}")
        result = await audit.verify()
        console.print(result.model_dump())
        return 0 if result.valid else 1
    finally:
        await session.aclose()
