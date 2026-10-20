"""``orqentis db`` and ``orqentis audit`` against PostgreSQL (the operator commands of ADR-0017)."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from rich.console import Console

from orqentis.bootstrap import open_postgres_session
from orqentis.cli import main
from orqentis.config.clock import SystemClock
from orqentis.db_commands import run_audit_postgres, run_db
from orqentis.storage.blob import BlobStorage
from tests.conftest import make_settings
from tests.support.postgres_app import AppStorage, azurite_blob_storage


def console() -> tuple[Console, io.StringIO]:
    buffer = io.StringIO()
    return Console(file=buffer, width=200, highlight=False), buffer


async def test_db_status_and_migrate(app_storage: AppStorage, tmp_path: Path) -> None:
    settings = app_storage.settings(tmp_path)
    out, text = console()
    assert await run_db(settings, "status", out) == 1  # nothing applied yet: a pending migration is not "current"
    assert "pending  0001_initial" in text.getvalue()

    out, text = console()
    assert await run_db(settings, "migrate", out) == 0
    assert "Applied 1 migration(s): 0001_initial" in text.getvalue()
    out, text = console()
    assert await run_db(settings, "migrate", out) == 0 and "up to date" in text.getvalue()
    out, text = console()
    assert await run_db(settings, "status", out) == 0 and "applied  0001_initial" in text.getvalue()


async def test_db_commands_need_the_postgres_backend(tmp_path: Path) -> None:
    out, text = console()
    assert await run_db(make_settings(tmp_path), "status", out) == 2
    assert "ORQENTIS_STORAGE_BACKEND must be postgres" in text.getvalue()


async def test_reseed_counters_raises_the_numbers_above_the_evidence_that_survived_a_restore(
    app_storage: AppStorage, tmp_path: Path
) -> None:
    """Evidence is not in the database backup, so a restore can leave counters behind it."""
    settings = app_storage.settings(tmp_path)
    session = await open_postgres_session(settings, SystemClock(), migrate=True)
    try:
        assert await session.storage.incidents.next_incident_id(2031) == "INC-2031-0001"
        for incident_id in ("INC-2031-0007", "INC-2031-0003", "INC-2032-0002", "not-an-incident"):
            await session.blob.evidence.put_text(incident_id, "report.md", "# old evidence")
        out, text = console()
        assert await run_db(settings, "reseed-counters", out) == 0
        assert "2031 -> 7" in text.getvalue() and "2032 -> 2" in text.getvalue()
        assert await session.storage.incidents.next_incident_id(2031) == "INC-2031-0008"  # not 0002: 0002 has evidence
        assert await session.storage.incidents.next_incident_id(2032) == "INC-2032-0003"
        out, text = console()
        assert (
            await run_db(settings, "reseed-counters", out) == 0 and "No incident counter was behind" in text.getvalue()
        )
    finally:
        await session.aclose()


async def test_audit_verification_and_a_new_epoch_from_the_command_line(
    app_storage: AppStorage, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = app_storage.settings(tmp_path)
    session = await open_postgres_session(settings, SystemClock(), migrate=True)
    try:
        for n in range(3):
            await session.storage.audit.record(actor="a", action=f"step-{n}", outcome="OK")
    finally:
        await session.aclose()
    out, text = console()
    assert await run_audit_postgres(settings, out, start_epoch_reason=None) == 0
    assert "'valid': True" in text.getvalue() and "'records': 3" in text.getvalue()
    # Opening an epoch is an operator action: the application's own identity cannot do it (read-only marker container).
    out, text = console()
    assert await run_audit_postgres(settings, out, start_epoch_reason="from the app") == 1
    assert "Audit epoch not started" in text.getvalue()
    monkeypatch.setattr(BlobStorage, "open", staticmethod(azurite_blob_storage(app_storage.azurite, operator=True)))
    out, text = console()
    assert await run_audit_postgres(settings, out, start_epoch_reason="restored to 07:00Z from server old") == 0
    assert "Started audit epoch (record 4)" in text.getvalue() and "restored to 07:00Z" in text.getvalue()


def test_cli_audit_start_epoch_is_for_postgres_only(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["audit", "--data-dir", str(tmp_path), "start-epoch", "--reason", "x"]) == 2
    assert "PostgreSQL audit chain only" in capsys.readouterr().out.replace("\n", " ")


def test_the_serve_command_refuses_deployed_environments_on_local_storage(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    served: list[object] = []
    monkeypatch.setattr("uvicorn.run", lambda *a, **k: served.append(a))
    monkeypatch.setenv("ORQENTIS_DEPLOYMENT_ENVIRONMENT", "dev")
    monkeypatch.setenv("ORQENTIS_API_AUTH_MODE", "easy_auth")
    assert main(["serve"]) == 2
    assert "requires ORQENTIS_STORAGE_BACKEND=postgres" in capsys.readouterr().err
    assert served == []
