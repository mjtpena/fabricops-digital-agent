"""The contracts file: JSON Schema, validation (every class of error), the loader and its typed errors."""

from __future__ import annotations

import copy
import json
import logging
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from orqentis.bootstrap import build_container
from orqentis.cli import main
from orqentis.config.settings import FabricMode, Settings
from orqentis.contracts import (
    ContractsError,
    ContractsFileNotFoundError,
    ContractsInvalidError,
    ContractsNotConfiguredError,
    EstateContracts,
    load_contracts,
)
from orqentis.contracts.catalog import CatalogInvalidError, CatalogSnapshot
from orqentis.contracts.diagnostics import MAX_FILE_BYTES
from orqentis.contracts.locator import locate
from orqentis.contracts.model import ESTATE_ONLY_KEYS
from orqentis.contracts.schema import contracts_json_schema, render_contracts_schema
from orqentis.contracts.validate import ValidationReport, validate_text
from orqentis.fabric.fixtures import EstateFixture
from tests.conftest import REPO

SCHEMA_PATH = REPO / "schemas" / "contracts.schema.json"
EXAMPLE = REPO / "contracts" / "estate.example.json"
CATALOG_EXAMPLE = REPO / "contracts" / "catalog.example.json"
ESTATE = REPO / "fixtures" / "fabric" / "estate.json"

WS = "00000000-0000-0000-0000-0000000000a1"
PIPELINE = "00000000-0000-0000-0000-0000000000b1"
LAKEHOUSE = "00000000-0000-0000-0000-0000000000b3"


def example() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    return loaded


def check(document: dict[str, Any] | str, *, catalog: bool = False, tenant_id: str | None = None) -> ValidationReport:
    text = document if isinstance(document, str) else json.dumps(document, indent=2)
    snapshot = CatalogSnapshot.load(CATALOG_EXAMPLE) if catalog else None
    return validate_text(text, Path("contracts.json"), catalog=snapshot, tenant_id=tenant_id)


def messages(report: ValidationReport, severity: str = "error") -> list[str]:
    return [f"{v.pointer}: {v.message}" for v in report.violations if v.severity == severity]


def errors_at(report: ValidationReport, pointer: str) -> list[str]:
    return [v.message for v in report.errors if v.pointer == pointer]


# ------------------------------------------------------------------------------------------ the schema
def test_committed_schema_matches_the_model() -> None:
    committed = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert committed == contracts_json_schema(), (
        "schemas/contracts.schema.json drifted from EstateContracts; regenerate it with: "
        "orqentis contracts schema --output schemas/contracts.schema.json"
    )


def test_committed_schema_file_is_the_rendered_one() -> None:
    assert SCHEMA_PATH.read_text(encoding="utf-8").replace("\r\n", "\n") == render_contracts_schema()


def test_schema_check_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["contracts", "schema", "--check", str(SCHEMA_PATH)]) == 0
    stale = tmp_path / "stale.json"
    stale.write_text(json.dumps({**contracts_json_schema(), "title": "something else"}), encoding="utf-8")
    assert main(["contracts", "schema", "--check", str(stale)]) == 1
    assert "out of date" in capsys.readouterr().out
    assert main(["contracts", "schema", "--check", str(tmp_path / "missing.json")]) == 1
    out = tmp_path / "written.json"
    assert main(["contracts", "schema", "--output", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8")) == contracts_json_schema()


def test_schema_is_a_valid_json_schema_that_accepts_the_example() -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    assert list(validator.iter_errors(example())) == []
    assert list(validator.iter_errors(json.loads(ESTATE.read_text(encoding="utf-8")))) != []  # estate-only sections


def test_schema_and_model_agree_on_the_environment_selector_rule() -> None:
    validator = Draft202012Validator(contracts_json_schema())

    def accepted_by_schema(entry: dict[str, Any]) -> bool:
        return not list(validator.iter_errors({"environments": [entry]}))

    def accepted_by_model(entry: dict[str, Any]) -> bool:
        return not check({"environments": [entry]}).errors

    for entry, expected in [
        ({"workspace_id": "a", "environment": "PROD"}, True),
        ({"workspace_name_pattern": "Sales*", "environment": "DEV"}, True),
        ({"item_id": "a", "environment": "TEST"}, True),
        ({"environment": "PROD"}, False),
        ({"workspace_id": "a", "item_id": "b", "environment": "PROD"}, False),
        ({"workspace_id": "a", "environment": "STAGING"}, False),
        ({"workspace_id": "a", "environment": "PROD", "colour": "red"}, False),
    ]:
        assert accepted_by_schema(entry) is expected, entry
        assert accepted_by_model(entry) is expected, entry


def test_estate_only_keys_are_exactly_the_estate_fixture_extras() -> None:
    assert set(EstateFixture.model_fields) - set(EstateContracts.model_fields) == ESTATE_ONLY_KEYS


# --------------------------------------------------------------------------------------- valid files
def test_example_passes_with_and_without_a_catalog() -> None:
    assert check(example(), catalog=True).violations == ()
    report = check(example())
    assert report.errors == ()
    assert [w.message for w in report.warnings] == [
        "7 reference(s) to Fabric items could not be checked because no catalog was supplied "
        "(use --catalog, or validate a full estate file)"
    ]


def test_full_estate_fixture_is_a_valid_contracts_file() -> None:
    assert main(["contracts", "validate", str(ESTATE)]) == 0  # its own workspaces/items are the catalog
    assert len(load_contracts(ESTATE).dependencies) == 31


def test_validate_cli_on_the_example(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["contracts", "validate", str(EXAMPLE), "--catalog", str(CATALOG_EXAMPLE), "--strict"]) == 0
    assert "0 error(s), 0 warning(s)" in capsys.readouterr().out
    assert main(["contracts", "validate", str(EXAMPLE)]) == 0  # a warning does not fail without --strict
    assert main(["contracts", "validate", str(EXAMPLE), "--strict"]) == 1


# ---------------------------------------------------------------------------- errors: syntax / schema
def test_invalid_json_reports_line_and_column() -> None:
    report = check('{\n  "slas": [\n    }\n}')
    assert report.contracts is None
    (violation,) = report.errors
    assert violation.message.startswith("invalid JSON")
    assert (violation.line, violation.column) == (3, 5)


def test_top_level_must_be_an_object() -> None:
    assert messages(check("[]")) == [": the contracts file must be a JSON object"]


def test_duplicate_json_keys_are_an_error() -> None:
    report = check('{\n  "slas": [],\n  "slas": []\n}')
    (violation,) = report.errors
    assert "duplicate key 'slas'" in violation.message
    assert (violation.line, violation.column) == (3, 3)


def test_unknown_field_missing_field_and_wrong_type_point_at_the_value() -> None:
    document = example()
    document["dependenciez"] = []
    del document["dependencies"][0]["target_item_id"]
    document["dependencies"][1]["dependency_type"] = "FEEDS"
    document["slas"][0]["owner"] = 7
    report = check(json.dumps(document, indent=2))
    assert errors_at(report, "/dependenciez") == ["unknown field (check the spelling against the schema)"]
    assert errors_at(report, "/dependencies/0/target_item_id") == ["Field required"]
    assert any("TRIGGERS" in m for m in errors_at(report, "/dependencies/1/dependency_type"))
    assert errors_at(report, "/slas/0/owner") == ["Input should be a valid string"]
    assert all(v.line is not None for v in report.errors)


def test_environment_selector_errors() -> None:
    both = check({"environments": [{"workspace_id": "a", "item_id": "b", "environment": "PROD"}]})
    assert errors_at(both, "/environments/0") == [
        "declare exactly one of workspace_id, workspace_name_pattern, item_id or capacity_id"
    ]
    neither = check({"environments": [{"environment": "PROD"}]})
    assert errors_at(neither, "/environments/0") == [
        "declare exactly one of workspace_id, workspace_name_pattern, item_id or capacity_id"
    ]
    bad_env = check({"environments": [{"workspace_id": "a", "environment": "STAGING"}]})
    assert errors_at(bad_env, "/environments/0/environment")


@pytest.mark.security
def test_violations_never_echo_values_from_the_file() -> None:
    document = example()
    document["slas"][0]["owner"] = "super-secret-owner@contoso.com"
    document["slas"][0]["timezone"] = "Not/AZone-with-secret-token-123"
    document["external_sources"][0]["tenant_id"] = {"password": "hunter2-hunter2"}
    report = check(document)
    rendered = "\n".join(v.format("contracts.json") for v in report.violations)
    assert report.errors
    for secret in ("super-secret-owner", "secret-token-123", "hunter2"):
        assert secret not in rendered


# ------------------------------------------------------------------ errors: internal consistency
def test_duplicate_ids_in_every_section() -> None:
    document = example()
    for section in ("external_sources", "data_products", "dependencies", "slas"):
        document[section].append(copy.deepcopy(document[section][0]))
    report = check(document)
    for section in ("external_sources", "data_products", "dependencies", "slas"):
        (message,) = errors_at(report, f"/{section}/{len(document[section]) - 1}/id") or [""]
        assert message.startswith("duplicate id '") and "first declared at line" in message, section


def test_a_data_product_and_a_source_cannot_share_an_id() -> None:
    document = example()
    document["data_products"][0]["id"] = "src-crm"
    assert any("also an external source" in m for m in errors_at(check(document), "/data_products/0/id"))


def test_duplicate_edges_and_self_edges() -> None:
    document = example()
    duplicate = copy.deepcopy(document["dependencies"][1])
    duplicate["id"] = "dep-again"
    document["dependencies"].append(duplicate)
    document["dependencies"].append({**document["dependencies"][0], "id": "dep-self", "target_item_id": PIPELINE})
    report = check(document)
    assert any("duplicate edge" in m and "each edge may appear once" in m for m in errors_at(report, "/dependencies/6"))
    assert errors_at(report, "/dependencies/7") == [f"'{PIPELINE}' depends on itself"]


def test_sla_references_timezone_and_weekdays() -> None:
    document = example()
    document["slas"][0]["data_product_id"] = "dp-missing"
    document["slas"][0]["timezone"] = "Mars/Olympus"
    document["slas"][0]["applies_on_weekdays"] = [0, 8, 3, 3]
    report = check(document)
    assert errors_at(report, "/slas/0/data_product_id") == [
        "data product 'dp-missing' is not declared in data_products"
    ]
    assert errors_at(report, "/slas/0/timezone") == ["not a valid IANA time zone (for example Australia/Sydney)"]
    assert errors_at(report, "/slas/0/applies_on_weekdays")


def test_duplicate_environment_selectors() -> None:
    document = {
        "environments": [
            {"workspace_id": "w", "environment": "PROD"},
            {"workspace_name_pattern": "Lab *", "environment": "DEV"},
            {"workspace_id": "w", "environment": "DEV"},
            {"workspace_name_pattern": "Lab *", "environment": "TEST"},
        ]
    }
    report = check(document)
    assert any("duplicate workspace_id 'w'" in m for m in errors_at(report, "/environments/2/workspace_id"))
    assert any(
        "duplicate workspace_name_pattern" in m for m in errors_at(report, "/environments/3/workspace_name_pattern")
    )


def test_tenant_ids_must_match_when_a_tenant_is_expected() -> None:
    assert check(example(), tenant_id="00000000-0000-0000-0000-000000000000").errors == ()
    report = check(example(), tenant_id="tenant-other")
    assert len(report.errors) == len(example()["dependencies"]) + 3  # source + product + sla + every edge
    assert all("does not match the expected tenant" in v.message for v in report.errors)


# ------------------------------------------------------------------------- errors: against a catalog
def test_references_are_checked_against_the_catalog() -> None:
    document = example()
    document["dependencies"][1]["target_item_id"] = "nb-ghost"
    document["data_products"][0]["backing_item_ids"] = ["lh-ghost"]
    document["data_products"][0]["workspace_id"] = "ws-ghost"
    document["slas"][0]["freshness_item_id"] = "pl-ghost"
    document["environments"].append({"workspace_id": "ws-ghost", "environment": "PROD"})
    document["environments"].append({"item_id": "item-ghost", "environment": "DEV"})
    report = check(document, catalog=True)
    assert any("'nb-ghost' is not a declared source" in m for m in errors_at(report, "/dependencies/1/target_item_id"))
    assert errors_at(report, "/data_products/0/backing_item_ids/0") == ["item 'lh-ghost' is not in the catalog"]
    assert errors_at(report, "/data_products/0/workspace_id") == ["workspace 'ws-ghost' is not in the catalog"]
    assert errors_at(report, "/slas/0/freshness_item_id") == ["item 'pl-ghost' is not in the catalog"]
    assert errors_at(report, "/environments/4/workspace_id") == ["workspace 'ws-ghost' is not in the catalog"]
    assert errors_at(report, "/environments/5/item_id") == ["item 'item-ghost' is not in the catalog"]


def test_a_declared_node_may_not_shadow_a_fabric_item() -> None:
    document = example()
    document["external_sources"][0]["id"] = PIPELINE
    document["data_products"][0]["id"] = LAKEHOUSE
    report = check(document, catalog=True)
    assert any("also a Fabric item" in m for m in errors_at(report, "/external_sources/0/id"))
    assert any("also a Fabric item" in m for m in errors_at(report, "/data_products/0/id"))


# ----------------------------------------------------------------------------------------- warnings
def test_unreviewed_inferred_unknown_and_draft_entries_are_warnings() -> None:
    document = {
        "environments": [
            {"workspace_id": "a", "environment": "PROD", "inferred": True},
            {"workspace_id": "b", "environment": "UNKNOWN"},
            {"workspace_name_pattern": "Nothing matches*", "environment": "DEV"},
        ],
        "external_sources": [
            {"id": "src", "tenant_id": "t", "workspace_id": WS, "display_name": "x", "metadata": {"draft": True}}
        ],
    }
    report = check(document, catalog=True)
    warnings = messages(report, "warning")
    assert any(m.startswith("/environments/0: drafted from naming conventions") for m in warnings)
    assert any(m.startswith("/environments/1/environment: environment is UNKNOWN") for m in warnings)
    assert any(
        m.startswith("/environments/2/workspace_name_pattern: the pattern matches no workspace") for m in warnings
    )
    assert any(m.startswith("/external_sources/0: draft placeholder 'src'") for m in warnings)
    assert not check(document).ok(strict=True)


def test_empty_file_and_mixed_tenants_warn() -> None:
    assert messages(check({}), "warning") == [": the file declares nothing (all sections are empty)"]
    document = example()
    document["slas"][0]["tenant_id"] = "another-tenant"
    assert any("2 different tenant_id values" in m for m in messages(check(document, catalog=True), "warning"))


def test_violation_format_is_line_oriented() -> None:
    text = json.dumps({"slas": [{"id": "s"}]}, indent=2)
    report = validate_text(text, Path("estate.json"))
    first = report.errors[0].format(Path("estate.json"))
    assert first.startswith("estate.json:") and ": error: /slas/0/" in first and first.count("\n") == 0


def test_validate_cli_prints_one_line_per_violation_and_exits_non_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    document = example()
    document["dependencies"][0]["dependency_type"] = "FEEDS"
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    assert main(["contracts", "validate", str(path)]) == 1
    lines = capsys.readouterr().out.splitlines()
    assert any(f"{path.name}:" in line and ": error: /dependencies/0/dependency_type: " in line for line in lines)
    assert lines[-1].endswith("(references to Fabric items were not checked against a catalog)")
    assert main(["contracts", "validate", str(tmp_path / "missing.json")]) == 1
    assert "Contracts file not found" in capsys.readouterr().out


def test_catalog_errors_are_typed(tmp_path: Path) -> None:
    bad = tmp_path / "catalog.json"
    bad.write_text(json.dumps({"workspaces": [{"id": "w"}]}), encoding="utf-8")
    with pytest.raises(CatalogInvalidError) as excinfo:
        CatalogSnapshot.load(bad)
    assert "Catalog file is invalid" in str(excinfo.value)
    assert main(["contracts", "validate", str(EXAMPLE), "--catalog", str(bad)]) == 1


# ------------------------------------------------------------------------------------ the locator
def test_locator_maps_pointers_to_positions() -> None:
    text = '{\n  "a": [\n    {"b": 1},\n    {"b": 2}\n  ],\n  "c/d": "x"\n}'
    lines = text.splitlines()
    where = locate(text)

    def text_at(pointer: str) -> str:
        line, column = where.positions[pointer]
        return lines[line - 1][column - 1 :]

    assert where.positions[""] == (1, 1)
    assert (where.positions["/a"], text_at("/a")) == ((2, 8), "[")
    assert (where.positions["/a/1"], text_at("/a/1")) == ((4, 5), '{"b": 2}')
    assert (where.positions["/a/1/b"], text_at("/a/1/b")) == ((4, 11), "2}")
    assert (where.positions["/c~1d"], text_at("/c~1d")) == ((6, 10), '"x"')
    assert where.find("/a/9/zzz") == (2, 8)  # nearest enclosing value
    assert where.find("/nothing") == (1, 1)


def test_locator_survives_odd_but_valid_json() -> None:
    assert locate("{}").positions[""] == (1, 1)
    assert locate('[[], {}, null, true, -1.5e3, "\\"q\\""]').positions["/5"][0] == 1
    deep = "[" * 200 + "]" * 200
    assert locate(deep).positions == {}  # too deep to index, but never an exception


# ------------------------------------------------------------------------------------ the loader (AC4)
def test_loader_returns_the_model_for_a_valid_file() -> None:
    contracts = load_contracts(EXAMPLE)
    assert len(contracts.environments) == 4 and contracts.environments[0].workspace_id == WS


def test_missing_file_raises_a_typed_error_naming_the_path(tmp_path: Path) -> None:
    path = tmp_path / "nowhere" / "estate.json"
    with pytest.raises(ContractsFileNotFoundError) as excinfo:
        load_contracts(path)
    assert excinfo.value.path == path
    assert str(path) in str(excinfo.value) and "does not exist" in str(excinfo.value)
    assert isinstance(excinfo.value, ContractsError)
    assert excinfo.value.to_dict()["path"] == str(path)


def test_directory_oversized_and_non_utf8_files_are_typed_errors(tmp_path: Path) -> None:
    with pytest.raises(ContractsInvalidError, match="not a file"):
        load_contracts(tmp_path)
    big = tmp_path / "big.json"
    big.write_bytes(b" " * (MAX_FILE_BYTES + 1))
    with pytest.raises(ContractsInvalidError, match="larger than") as excinfo:
        load_contracts(big)
    assert excinfo.value.path == big and "Contracts file is invalid" in str(excinfo.value)
    binary = tmp_path / "binary.json"
    binary.write_bytes(b"\xff\xfe\x00bad")
    with pytest.raises(ContractsInvalidError, match="UTF-8"):
        load_contracts(binary)


def test_invalid_file_raises_a_typed_error_with_the_path_and_every_violation(tmp_path: Path) -> None:
    document = example()
    document["dependencies"][0]["dependency_type"] = "FEEDS"
    document["slas"][0]["owner"] = 7
    document["dependenciez"] = []
    path = tmp_path / "estate.json"
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    with pytest.raises(ContractsInvalidError) as excinfo:
        load_contracts(path)
    error = excinfo.value
    assert error.path == path
    assert {v.pointer for v in error.violations} == {
        "/dependencies/0/dependency_type",
        "/slas/0/owner",
        "/dependenciez",
    }
    rendered = str(error)
    assert str(path) in rendered
    for violation in error.violations:
        assert violation.pointer in rendered and violation.line is not None
    assert error.to_dict()["violations"][0]["line"] == error.violations[0].line


def test_consistency_errors_are_reported_once_the_schema_passes(tmp_path: Path) -> None:
    document = example()
    document["slas"][0]["timezone"] = "Mars/Olympus"
    document["slas"][0]["data_product_id"] = "dp-missing"
    path = tmp_path / "estate.json"
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    with pytest.raises(ContractsInvalidError) as excinfo:
        load_contracts(path)
    assert {v.pointer for v in excinfo.value.violations} == {"/slas/0/timezone", "/slas/0/data_product_id"}


def test_a_syntax_error_is_a_typed_error_with_a_position(tmp_path: Path) -> None:
    path = tmp_path / "estate.json"
    path.write_text('{"slas": [,]}', encoding="utf-8")
    with pytest.raises(ContractsInvalidError) as excinfo:
        load_contracts(path)
    assert excinfo.value.violations[0].line == 1
    assert "invalid JSON" in str(excinfo.value)


def test_error_text_is_bounded_but_keeps_every_violation(tmp_path: Path) -> None:
    document = {"slas": [{"id": f"s{i}"} for i in range(40)]}
    path = tmp_path / "estate.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ContractsInvalidError) as excinfo:
        load_contracts(path)
    assert len(excinfo.value.violations) > 25
    assert "... and " in str(excinfo.value) and " more" in str(excinfo.value)


def test_warnings_do_not_stop_loading_and_only_their_count_is_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    document = example()
    document["environments"][0]["inferred"] = True
    document["external_sources"][0]["owner"] = "private-owner@contoso.com"
    path = tmp_path / "estate.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    logger = logging.getLogger("orqentis")
    attach = not logger.propagate  # configure_logging() may have set propagate=False in an earlier test
    if attach:
        logger.addHandler(caplog.handler)
    caplog.set_level(logging.DEBUG, logger="orqentis")
    try:
        contracts = load_contracts(path)
    finally:
        if attach:
            logger.removeHandler(caplog.handler)
    assert contracts.environments[0].inferred is True
    records = [r for r in caplog.records if "contracts loaded with warnings" in r.getMessage()]
    assert len(records) == 1 and records[0].__dict__["warning_count"] >= 1
    assert "private-owner" not in caplog.text


# ----------------------------------------------------------------- startup (no crash loop on bad input)
def rest_settings(tmp_path: Path, contracts_path: Path | None) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        fabric_mode=FabricMode.REST,
        tenant_id="tenant-test",
        contracts_path=contracts_path,
        data_dir=tmp_path / "var",
        runbooks_dir=REPO / "runbooks",
        fixtures_dir=REPO / "fixtures",
    )


async def test_rest_startup_without_a_contracts_path_raises_the_typed_error(tmp_path: Path) -> None:
    with pytest.raises(ContractsNotConfiguredError) as excinfo:
        await build_container(rest_settings(tmp_path, None))
    assert "ORQENTIS_CONTRACTS_PATH" in str(excinfo.value)


async def test_rest_startup_with_a_missing_or_invalid_file_raises_the_typed_error(tmp_path: Path) -> None:
    with pytest.raises(ContractsFileNotFoundError, match=r"nowhere.json"):
        await build_container(rest_settings(tmp_path, tmp_path / "nowhere.json"))
    invalid = tmp_path / "invalid.json"
    invalid.write_text('{"slas": [{"id": "only-an-id"}]}', encoding="utf-8")
    with pytest.raises(ContractsInvalidError) as excinfo:
        await build_container(rest_settings(tmp_path, invalid))
    assert str(invalid) in str(excinfo.value) and "/slas/0/tenant_id" in str(excinfo.value)
