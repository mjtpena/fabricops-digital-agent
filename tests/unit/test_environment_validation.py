"""``contracts validate`` makes environment downgrades and unreviewed entries visible; the resolver ignores drafts."""

from __future__ import annotations

import json
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from orqentis.contracts.catalog import CatalogSnapshot
from orqentis.contracts.schema import contracts_json_schema
from orqentis.contracts.validate import ValidationReport, validate_text
from orqentis.domain.common import Environment
from orqentis.domain.estate import (
    Capacity,
    EnvironmentDeclaration,
    ExternalSource,
    Lakehouse,
    Notebook,
    Pipeline,
    Workspace,
)
from orqentis.topology.environment import EnvironmentResolver, EnvironmentSource, is_downgrade

PROD, TEST, DEV, UNKNOWN = Environment.PROD, Environment.TEST, Environment.DEV, Environment.UNKNOWN
T = "tenant-test"


def catalog() -> CatalogSnapshot:
    return CatalogSnapshot.of(
        [
            Workspace(id="ws-sales", tenant_id=T, display_name="Sales [PROD]"),
            Workspace(id="ws-lab", tenant_id=T, display_name="Lab [DEV]"),
            Workspace(id="ws-plain", tenant_id=T, display_name="Shared Services"),
        ],
        [
            Pipeline(id="pl-sales", tenant_id=T, workspace_id="ws-sales", display_name="ingest"),
            Notebook(id="nb-sales", tenant_id=T, workspace_id="ws-sales", display_name="explore"),
            Lakehouse(id="lh-lab", tenant_id=T, workspace_id="ws-lab", display_name="LabLake"),
        ],
    )


def check(document: dict[str, Any], *, with_catalog: bool = True) -> ValidationReport:
    return validate_text(json.dumps(document), catalog=catalog() if with_catalog else None)


def warnings(report: ValidationReport, pointer_prefix: str = "") -> list[str]:
    return [v.message for v in report.warnings if v.pointer.startswith(pointer_prefix)]


def entry(environment: str, **selector: Any) -> dict[str, Any]:
    return {"environment": environment, **selector}


# ------------------------------------------------------------------------------------- downgrades
def test_declaring_a_prod_workspace_lower_warns() -> None:
    report = check({"environments": [entry("DEV", workspace_id="ws-sales")]})
    assert report.errors == ()
    (message,) = warnings(report, "/environments/0")
    assert "lowers 1 PROD workspace(s), item(s) or capacity(ies) ('ws-sales')" in message
    assert "POL-002" in message and "less severe" in message
    assert not report.ok(strict=True)  # --strict is how CI refuses an unconfirmed downgrade


def test_a_confirmed_downgrade_is_not_warned_about() -> None:
    document = {"environments": [{**entry("TEST", workspace_id="ws-sales"), "accepts_downgrade": True}]}
    report = check(document)
    assert report.violations == ()
    assert report.ok(strict=True)


def test_a_catch_all_pattern_warns_and_lists_what_it_lowers() -> None:
    report = check({"environments": [entry("DEV", workspace_name_pattern="*")]})
    messages = warnings(report, "/environments/0")
    assert any("catch-all pattern" in m and "EVERY workspace" in m for m in messages)
    assert any("lowers 1 PROD workspace(s)" in m and "'ws-sales'" in m for m in messages)
    # The catch-all warning is raised even without a catalog, because the pattern alone is the problem.
    assert any(
        "catch-all" in m
        for m in warnings(check({"environments": [entry("PROD", workspace_name_pattern="**")]}, with_catalog=False))
    )
    # Confirming the downgrade does not make a catch-all acceptable.
    confirmed = {"environments": [{**entry("DEV", workspace_name_pattern="*"), "accepts_downgrade": True}]}
    assert any("catch-all" in m for m in warnings(check(confirmed)))


def test_an_item_declared_lower_than_its_prod_workspace_warns() -> None:
    report = check({"environments": [entry("DEV", item_id="nb-sales")]})
    (message,) = warnings(report, "/environments/0")
    assert "lowers 1 PROD workspace(s), item(s) or capacity(ies) ('nb-sales')" in message


@pytest.mark.parametrize(
    "declaration",
    [
        entry("PROD", workspace_id="ws-sales"),  # same as the name
        entry("PROD", workspace_id="ws-lab"),  # an upgrade
        entry("DEV", workspace_id="ws-plain"),  # the name says nothing: a first classification, not a downgrade
        entry("TEST", workspace_id="ws-lab"),
    ],
)
def test_other_changes_are_not_downgrades(declaration: dict[str, Any]) -> None:
    assert warnings(check({"environments": [declaration]})) == []


def test_unreviewed_and_unknown_entries_are_not_downgrades_because_they_are_not_enforced() -> None:
    draft = check({"environments": [{**entry("DEV", workspace_id="ws-sales"), "inferred": True}]})
    messages = warnings(draft, "/environments/0")
    assert len(messages) == 1 and "NOT enforced until reviewed" in messages[0]
    unknown = check({"environments": [entry("UNKNOWN", workspace_id="ws-sales")]})
    assert any("UNKNOWN, so this entry is NOT enforced" in m for m in warnings(unknown))
    assert not any("lowers" in m for m in warnings(unknown))


def test_a_name_pattern_downgrade_is_attributed_to_the_pattern_that_decided() -> None:
    document = {
        "environments": [
            entry("PROD", workspace_id="ws-sales"),  # wins for ws-sales (id beats pattern)
            entry("DEV", workspace_name_pattern="S*"),  # would lower it, but never applies to ws-sales
        ]
    }
    assert warnings(check(document), "/environments/1") == []


# ------------------------------------------------------------- data products and sources vs workspace
def test_a_source_or_product_disagreeing_with_its_workspace_environment_warns() -> None:
    document = {
        "external_sources": [
            {"id": "src-1", "tenant_id": T, "workspace_id": "ws-sales", "display_name": "CRM", "environment": "DEV"},
            {"id": "src-2", "tenant_id": T, "workspace_id": "ws-sales", "display_name": "ERP", "environment": "PROD"},
            {"id": "src-3", "tenant_id": T, "workspace_id": "ws-sales", "display_name": "WMS"},  # UNKNOWN: no claim
        ]
    }
    report = check(document)
    assert report.errors == ()
    assert warnings(report, "/external_sources/0/environment") == ["'src-1' is DEV but its workspace resolves to PROD"]
    assert warnings(report, "/external_sources/1") == [] and warnings(report, "/external_sources/2") == []


def test_the_comparison_uses_the_declared_environment_of_the_workspace() -> None:
    document = {
        "environments": [entry("DEV", workspace_id="ws-sales")],
        "data_products": [
            {
                "id": "dp-1",
                "tenant_id": T,
                "workspace_id": "ws-sales",
                "display_name": "x",
                "owner": "o",
                "environment": "DEV",
            }
        ],
    }
    assert warnings(check(document), "/data_products") == []


# ------------------------------------------------------------------------------------- capacity ids
def test_capacity_declarations_are_accepted_checked_for_duplicates_and_in_the_schema() -> None:
    document = {"environments": [entry("PROD", capacity_id="cap-1"), entry("DEV", capacity_id="cap-1")]}
    report = check(document, with_catalog=False)
    assert [v.pointer for v in report.errors] == ["/environments/1/capacity_id"]
    assert "duplicate capacity_id 'cap-1'" in report.errors[0].message
    validator = Draft202012Validator(contracts_json_schema())
    assert not list(validator.iter_errors({"environments": [entry("PROD", capacity_id="cap-1")]}))
    assert list(validator.iter_errors({"environments": [entry("PROD", capacity_id="cap-1", item_id="x")]}))
    assert not list(validator.iter_errors({"environments": [{**entry("DEV", item_id="x"), "accepts_downgrade": True}]}))


# ---------------------------------------------------------------------------------------- resolver
def test_unreviewed_and_unknown_declarations_fall_through_to_the_name() -> None:
    resolver = EnvironmentResolver(
        [
            EnvironmentDeclaration(workspace_id="ws-1", environment=DEV, inferred=True),
            EnvironmentDeclaration(workspace_name_pattern="Finance *", environment=UNKNOWN),
            EnvironmentDeclaration(item_id="nb-1", environment=DEV, inferred=True),
            EnvironmentDeclaration(capacity_id="cap-1", environment=UNKNOWN),
        ]
    )
    assert not resolver.has_declarations  # nothing here is enforced
    by_id = resolver.resolve_workspace("ws-1", "Sales [PROD]")
    assert (by_id.environment, by_id.source) == (PROD, EnvironmentSource.DRAFT_IGNORED)
    by_pattern = resolver.resolve_workspace("ws-2", "Finance [PROD]")
    assert (by_pattern.environment, by_pattern.source) == (PROD, EnvironmentSource.DRAFT_IGNORED)
    untouched = resolver.resolve_workspace("ws-3", "Other [TEST]")
    assert (untouched.environment, untouched.source) == (TEST, EnvironmentSource.INFERRED)
    item = resolver.resolve_item("nb-1", PROD)
    assert (item.environment, item.source) == (PROD, EnvironmentSource.DRAFT_IGNORED)
    capacity = resolver.resolve_capacity("cap-1", "Prod F64")
    assert (capacity.environment, capacity.source) == (PROD, EnvironmentSource.DRAFT_IGNORED)


def test_the_enforced_flag() -> None:
    assert EnvironmentDeclaration(workspace_id="a", environment=PROD).enforced
    assert not EnvironmentDeclaration(workspace_id="a", environment=PROD, inferred=True).enforced
    assert not EnvironmentDeclaration(workspace_id="a", environment=UNKNOWN).enforced


def test_resolver_reports_which_declaration_decided() -> None:
    resolver = EnvironmentResolver(
        [
            EnvironmentDeclaration(workspace_id="ws-1", environment=TEST, inferred=True),
            EnvironmentDeclaration(workspace_name_pattern="Sales*", environment=TEST),
            EnvironmentDeclaration(workspace_id="ws-1", environment=DEV),
        ]
    )
    assert resolver.resolve_workspace("ws-1", "Sales [PROD]").declaration_index == 2  # id beats pattern
    assert resolver.resolve_workspace("ws-2", "Sales [PROD]").declaration_index == 1
    assert resolver.resolve_workspace("ws-3", "Other").declaration_index is None
    declared = resolver.resolve_declared_workspace("ws-1", None)
    assert declared is not None and declared.environment is DEV
    assert resolver.resolve_declared_workspace("ws-3", "Other") is None


def test_is_downgrade() -> None:
    assert is_downgrade(PROD, DEV) and is_downgrade(PROD, TEST)
    assert not is_downgrade(PROD, PROD) and not is_downgrade(PROD, UNKNOWN)
    assert not is_downgrade(TEST, DEV) and not is_downgrade(DEV, PROD) and not is_downgrade(UNKNOWN, DEV)


def test_external_source_model_is_unaffected() -> None:
    assert ExternalSource(id="s", tenant_id=T, workspace_id="w", display_name="d").environment is UNKNOWN


# ------------------------------------------------------------------------------------------ capacities
def catalog_with_capacities() -> CatalogSnapshot:
    base = catalog()
    capacities = [
        Capacity(id="cap-prod", tenant_id=T, display_name="Prod F64", sku="F64"),
        Capacity(id="cap-shared", tenant_id=T, display_name="Shared F8", sku="F8"),
    ]
    return CatalogSnapshot.of(list(base.workspaces.values()), list(base.items.values()), capacities)


def check_capacities(document: dict[str, Any]) -> ValidationReport:
    return validate_text(json.dumps(document), catalog=catalog_with_capacities())


def test_lowering_a_prod_capacity_warns_like_any_other_downgrade() -> None:
    report = check_capacities({"environments": [entry("DEV", capacity_id="cap-prod")]})
    (message,) = warnings(report, "/environments/0")
    assert "lowers 1 PROD workspace(s), item(s) or capacity(ies) ('cap-prod')" in message
    confirmed = {"environments": [{**entry("DEV", capacity_id="cap-prod"), "accepts_downgrade": True}]}
    assert check_capacities(confirmed).violations == ()


def test_naming_an_unnamed_capacity_is_not_a_downgrade() -> None:
    assert warnings(check_capacities({"environments": [entry("PROD", capacity_id="cap-shared")]})) == []


def test_a_capacity_declaration_must_exist_when_the_catalog_lists_capacities() -> None:
    report = check_capacities({"environments": [entry("PROD", capacity_id="cap-ghost")]})
    assert [v.message for v in report.errors] == ["capacity 'cap-ghost' is not in the catalog"]
    # A catalog that carries no capacities cannot say, so it does not guess.
    assert check({"environments": [entry("PROD", capacity_id="cap-ghost")]}).errors == ()
