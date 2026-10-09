"""Runbook loading, validation and retrieval."""

from __future__ import annotations

from pathlib import Path

import pytest

from orqentis.domain.common import ItemType
from orqentis.domain.runbooks import REQUIRED_RUNBOOK_SECTIONS
from orqentis.runbooks.catalog import RunbookCatalog, RunbookQuery
from orqentis.runbooks.loader import RunbookValidationError, load_runbooks, parse_runbook
from tests.conftest import REPO

VALID = """---
id: RB-TEST-001
version: 1.0
title: Test runbook
category: PIPELINE_TIMEOUT
severity: LOW
signals: ["timed out"]
applies_to: [DataPipeline]
risk: LOW_RISK
approval_required: true
owner: me@x
last_reviewed: 2026-01-01
---
# Test
""" + "\n".join(f"## {s}\n- step for {s}\n" for s in REQUIRED_RUNBOOK_SECTIONS)


@pytest.fixture(scope="module")
def catalog() -> RunbookCatalog:
    return RunbookCatalog.load(REPO / "runbooks")


def test_all_repository_runbooks_are_valid(catalog: RunbookCatalog) -> None:
    ids = {r.id for r in catalog.all()}
    assert {
        "RB-SCHEMA-DRIFT-001",
        "RB-AUTH-FAILURE-001",
        "RB-CAPACITY-THROTTLING-001",
        "RB-LAKEHOUSE-WRITE-001",
        "RB-PIPELINE-TIMEOUT-001",
        "RB-SM-REFRESH-FAILURE-001",
        "RB-SM-STALE-001",
    } == ids
    for runbook in catalog.all():
        assert set(REQUIRED_RUNBOOK_SECTIONS) <= set(runbook.sections)
        assert len(runbook.content_hash) == 64


def test_schema_drift_runbook_is_retrieved_for_schema_error(catalog: RunbookCatalog) -> None:
    matches = catalog.search(
        RunbookQuery(
            category="SCHEMA_DRIFT",
            text='[UNRESOLVED_COLUMN] expected column "customer_id" is missing',
            item_types=[ItemType.NOTEBOOK],
        )
    )
    assert matches[0].runbook_id == "RB-SCHEMA-DRIFT-001"
    assert matches[0].score >= 0.75
    assert "category SCHEMA_DRIFT" in matches[0].reasons


def test_signals_alone_can_match(catalog: RunbookCatalog) -> None:
    matches = catalog.search(
        RunbookQuery(text="CapacityLimitExceeded: background operations are being rejected", min_score=0.1)
    )
    assert matches and matches[0].runbook_id == "RB-CAPACITY-THROTTLING-001"


def test_no_match_for_unrelated_text(catalog: RunbookCatalog) -> None:
    assert catalog.search(RunbookQuery(category="UNKNOWN", text="internal error 0x80131500")) == []


def test_section_steps_strip_list_markers(catalog: RunbookCatalog) -> None:
    runbook = catalog.get("RB-SCHEMA-DRIFT-001")
    assert runbook is not None
    steps = runbook.section_steps("Recommended Remediation")
    assert steps[0].startswith("Agree the change with the source owner")
    assert [a.action_type.value for a in runbook.remediation_actions] == ["MODIFY_ITEM_DEFINITION", "RERUN_PIPELINE"]


def test_parse_valid_runbook() -> None:
    runbook = parse_runbook(VALID, relative_path="test.md")
    assert runbook.version == "1.0"
    assert runbook.section_steps("Validation") == ["step for Validation"]


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda t: t.replace("## Rollback\n- step for Rollback\n", ""), "Rollback"),
        (lambda t: t.replace('signals: ["timed out"]', 'signals: ["(unclosed"]'), "invalid signal"),
        (lambda t: t.replace("---\nid", "id", 1), "front matter"),
        (lambda t: t.replace("category: PIPELINE_TIMEOUT", "category: MADE_UP"), "Unknown incident category"),
    ],
)
def test_invalid_runbooks_are_rejected(mutate: object, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_runbook(mutate(VALID), relative_path="bad.md")  # type: ignore[operator]


def test_duplicate_ids_are_rejected(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text(VALID)
    (tmp_path / "b.md").write_text(VALID)
    with pytest.raises(RunbookValidationError, match="duplicate runbook id RB-TEST-001"):
        load_runbooks(tmp_path)
