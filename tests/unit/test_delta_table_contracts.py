"""``delta_tables``: the tables whose _delta_log FabricOps reads, declared in the contracts and checked."""

from __future__ import annotations

import json
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from orqentis.contracts.catalog import CatalogSnapshot
from orqentis.contracts.schema import contracts_json_schema
from orqentis.contracts.validate import ValidationReport, validate_text
from orqentis.domain.common import Environment
from orqentis.domain.estate import Lakehouse, Notebook, Workspace
from orqentis.fabric.fixtures import EstateFixture
from orqentis.topology.catalog import CatalogDiff
from tests.conftest import REPO

WS = Workspace(id="ws-1", tenant_id="t", display_name="Sales [PROD]", environment=Environment.PROD)
LH = Lakehouse(id="lh-1", tenant_id="t", workspace_id="ws-1", display_name="bronze", environment=Environment.PROD)
NB = Notebook(id="nb-1", tenant_id="t", workspace_id="ws-1", display_name="transform", environment=Environment.PROD)


def validate(tables: list[dict[str, Any]], *, catalog: bool = False) -> ValidationReport:
    text = json.dumps({"delta_tables": tables})
    snapshot = CatalogSnapshot.of([WS], [LH, NB]) if catalog else None
    return validate_text(text, "contracts.json", catalog=snapshot)


def messages(report: ValidationReport) -> str:
    return "\n".join(v.message for v in report.violations)


def test_a_declared_table_is_valid() -> None:
    report = validate([{"id": "t1", "item_id": "lh-1", "table": "customers", "schema_name": "dbo", "description": "d"}])
    assert report.ok(strict=False) and report.contracts is not None
    assert report.contracts.delta_tables[0].table == "customers"


@pytest.mark.parametrize("name", ["../x", "a/b", "..", ".hidden", "a b", "", "x" * 200, "a\\b"])
def test_a_table_name_cannot_carry_a_path(name: str) -> None:
    report = validate([{"id": "t1", "item_id": "lh-1", "table": name}])
    assert report.errors, name


def test_unknown_keys_are_refused_like_everywhere_else() -> None:
    assert validate([{"id": "t1", "item_id": "lh-1", "table": "c", "path": "Files/secret"}]).errors


def test_duplicates_are_errors() -> None:
    report = validate(
        [
            {"id": "t1", "item_id": "lh-1", "table": "customers"},
            {"id": "t1", "item_id": "lh-1", "table": "orders"},
            {"id": "t2", "item_id": "lh-1", "table": "customers"},
        ]
    )
    text = messages(report)
    assert "duplicate id 't1'" in text and "table 'customers' is already declared" in text


def test_with_a_catalog_the_lakehouse_must_exist_and_be_a_lakehouse() -> None:
    report = validate(
        [
            {"id": "ok", "item_id": "lh-1", "table": "customers"},
            {"id": "missing", "item_id": "lh-404", "table": "customers"},
            {"id": "wrong", "item_id": "nb-1", "table": "customers"},
        ],
        catalog=True,
    )
    text = messages(report)
    assert "lakehouse 'lh-404' is not in the catalog" in text
    assert "'nb-1' is a Notebook, not a Lakehouse" in text
    assert len(report.errors) == 2


def test_the_json_schema_describes_the_section() -> None:
    schema = contracts_json_schema()
    validator = Draft202012Validator(schema)
    assert not list(validator.iter_errors({"delta_tables": [{"id": "t", "item_id": "i", "table": "customers"}]}))
    assert list(validator.iter_errors({"delta_tables": [{"id": "t", "item_id": "i", "table": "../etc"}]}))
    assert "delta_tables" in schema["properties"]


def test_the_synthetic_estate_declares_one_and_a_catalog_refresh_sees_changes() -> None:
    estate = EstateFixture.model_validate_json(
        (REPO / "fixtures" / "fabric" / "estate.json").read_text(encoding="utf-8")
    )
    assert [(t.id, t.item_id, t.table) for t in estate.delta_tables] == [
        ("tbl-sales-orders-raw", "lh-sales-bronze", "sales_orders_raw")
    ]
    assert "delta_tables" in CatalogDiff.model_fields
