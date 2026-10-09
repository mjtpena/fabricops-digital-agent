"""`make demo` equivalent: the CLI runs the full synthetic incident without credentials."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from rich.console import Console

from orqentis.cli import main, run_demo

pytestmark = pytest.mark.scenario


async def test_demo_runs_end_to_end(tmp_path: Path) -> None:
    buffer = io.StringIO()
    console = Console(file=buffer, width=160, color_system=None)
    assert await run_demo("schema_drift", tmp_path / "demo", show_report=True, console=console) == 0
    output = buffer.getvalue()
    for expected in (
        "Simulated telemetry loaded",
        "Incident detected",
        "Evidence collected",
        "Investigation executed",
        "Dependency impact calculated",
        "Runbook matched",
        "Recommendation produced",
        "Incident report rendered",
        "Audit trace persisted",
        "hash chain valid",
        "INC-2026-0042",
        "RB-SCHEMA-DRIFT-001",
        "ACTION_REQUIRES_APPROVAL",
        "PRINCIPAL_NOT_HUMAN",
        "ACTION_DENIED",
        "Incident Report",
    ):
        assert expected in output, expected
    assert (tmp_path / "demo" / "evidence" / "INC-2026-0042" / "report.md").exists()
    assert (tmp_path / "demo" / "audit" / "audit.jsonl").exists()


def test_cli_utilities(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["scenarios"]) == 0
    assert main(["runbooks"]) == 0
    out = capsys.readouterr().out
    assert "schema_drift" in out and "7 runbooks valid" in out
