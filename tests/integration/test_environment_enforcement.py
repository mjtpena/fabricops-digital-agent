"""Only reviewed declarations are enforced; items, capacities and deployment stages follow declarations too."""

from __future__ import annotations

from orqentis.config.clock import FixedClock
from orqentis.contracts import EstateContracts
from orqentis.domain.actions import PolicyEffect
from orqentis.domain.common import Environment
from orqentis.domain.estate import EnvironmentDeclaration
from orqentis.domain.taxonomy import Severity
from orqentis.policies.engine import PolicyEngine
from orqentis.topology.catalog import EstateCatalog
from tests.integration.test_declared_environments import (
    FABRIC_PATH,
    NOW,
    change_permissions,
    client_for,
    collect_severities,
    estate_router,
)
from tests.integration.test_fabric_rest_client import Router, respond

PROD, TEST, DEV, UNKNOWN = Environment.PROD, Environment.TEST, Environment.DEV, Environment.UNKNOWN


def contracts_with(*declarations: EnvironmentDeclaration) -> EstateContracts:
    return EstateContracts(environments=list(declarations))


async def load(contracts: EstateContracts) -> tuple[EstateCatalog, dict[str, Severity]]:
    fabric = client_for(contracts)
    try:
        catalog = await EstateCatalog.load(fabric, contracts)
        return catalog, await collect_severities(fabric, catalog)
    finally:
        await fabric.aclose()


# ---------------------------------------------------------------- unreviewed drafts are never enforced
async def test_a_stale_scaffold_draft_cannot_pin_a_renamed_workspace_to_dev() -> None:
    # The draft said DEV when the workspace was called "Lab [DEV]"; it has since been renamed "Lab [PROD]".
    stale = EnvironmentDeclaration(workspace_id="ws-d", environment=DEV, inferred=True)
    catalog, severities = await load(contracts_with(stale))
    assert catalog.workspaces["ws-d"].environment is PROD
    assert catalog.workspaces["ws-d"].metadata["environment_source"] == "draft_ignored"
    assert catalog.items["pl-d"].environment is PROD
    assert severities["pl-d"] is Severity.HIGH
    # POL-002 still denies production permission changes there: the draft did not switch it off.
    decision = PolicyEngine.default(FixedClock(NOW)).evaluate(change_permissions(catalog, "pl-d"))
    assert decision.effect is PolicyEffect.DENY
    assert "POL-002-deny-production-permissions" in decision.matched_rule_ids


async def test_an_unknown_declaration_cannot_mask_a_production_name() -> None:
    masking = EnvironmentDeclaration(workspace_name_pattern="Lab *", environment=UNKNOWN)
    catalog, severities = await load(contracts_with(masking))
    assert catalog.workspaces["ws-d"].environment is PROD
    assert catalog.workspaces["ws-d"].metadata["environment_source"] == "draft_ignored"
    assert severities["pl-d"] is Severity.HIGH
    decision = PolicyEngine.default(FixedClock(NOW)).evaluate(change_permissions(catalog, "pl-d"))
    assert decision.effect is PolicyEffect.DENY


async def test_an_inferred_catch_all_pattern_changes_nothing() -> None:
    draft = EnvironmentDeclaration(workspace_name_pattern="*", environment=DEV, inferred=True)
    catalog, _ = await load(contracts_with(draft))
    assert {w.id: w.environment for w in catalog.workspaces.values()} == {
        "ws-a": DEV,  # "Sales [DEV]"
        "ws-b": UNKNOWN,
        "ws-c": TEST,
        "ws-d": PROD,
    }
    assert all(w.metadata["environment_source"] == "draft_ignored" for w in catalog.workspaces.values())


async def test_a_reviewed_declaration_still_applies_after_an_ignored_one() -> None:
    catalog, _ = await load(
        contracts_with(
            EnvironmentDeclaration(workspace_id="ws-a", environment=TEST, inferred=True),
            EnvironmentDeclaration(workspace_id="ws-a", environment=PROD),
        )
    )
    assert catalog.workspaces["ws-a"].environment is PROD
    assert catalog.workspaces["ws-a"].metadata["environment_source"] == "declared_id"


async def test_unreviewed_item_declarations_are_not_enforced_either() -> None:
    catalog, _ = await load(contracts_with(EnvironmentDeclaration(item_id="pl-d", environment=DEV, inferred=True)))
    assert catalog.items["pl-d"].environment is PROD
    assert catalog.items["pl-d"].metadata["environment_source"] == "draft_ignored"


# ------------------------------------------------------------------ item declarations reach job runs
async def test_pipeline_runs_honour_item_declarations() -> None:
    contracts = contracts_with(EnvironmentDeclaration(item_id="pl-d", environment=DEV, accepts_downgrade=True))
    fabric = client_for(contracts)
    try:
        catalog = await EstateCatalog.load(fabric, contracts)
        runs = await fabric.jobs.list_job_instances("ws-d", "pl-d")
        plain = await fabric.jobs.list_job_instances("ws-a", "pl-a")
        severities = await collect_severities(fabric, catalog)
    finally:
        await fabric.aclose()
    assert catalog.workspaces["ws-d"].environment is PROD
    assert catalog.items["pl-d"].environment is DEV
    assert catalog.items["pl-d"].metadata["environment_source"] == "declared_item"
    assert [r.environment for r in runs] == [DEV]  # the run carries the item's environment, not its workspace's
    assert [r.environment for r in plain] == [DEV]  # untouched item: its workspace is "Sales [DEV]"
    assert severities["pl-d"] is Severity.LOW


# --------------------------------------------------------------------------------------- capacities
CAPACITIES = [
    {"id": "cap-1", "displayName": "Shared F64", "sku": "F64", "state": "Active"},
    {"id": "cap-2", "displayName": "Prod F128", "sku": "F128", "state": "Active"},
    {"id": "cap-3", "displayName": "Lab F2", "sku": "F2", "state": "Active"},
]


async def test_capacity_environments_follow_declarations() -> None:
    contracts = contracts_with(
        EnvironmentDeclaration(capacity_id="cap-1", environment=PROD),
        EnvironmentDeclaration(capacity_id="cap-3", environment=DEV, inferred=True),  # a draft: ignored
    )
    fabric = client_for(contracts, router=estate_router(CAPACITIES))
    try:
        capacities = {c.id: c for c in await fabric.capacities.list_capacities()}
    finally:
        await fabric.aclose()
    assert capacities["cap-1"].environment is PROD  # the name says nothing; declared
    assert capacities["cap-1"].metadata["environment_source"] == "declared_capacity"
    assert capacities["cap-2"].environment is PROD  # inferred from "Prod"
    assert "environment_source" not in capacities["cap-2"].metadata
    assert capacities["cap-3"].environment is UNKNOWN  # the draft did not apply
    assert capacities["cap-3"].metadata["environment_source"] == "draft_ignored"


# -------------------------------------------------------------------------------- deployment stages
def deployment_routes(router: Router) -> None:
    dp = f"{FABRIC_PATH}/deploymentPipelines"
    router.add("GET", dp, respond(body={"value": [{"id": "dp-1", "displayName": "Sales"}]}))
    router.add(
        "GET",
        f"{dp}/dp-1/stages",
        respond(
            body={
                "value": [
                    {
                        "id": "st-1",
                        "displayName": "Stage A",
                        "order": 0,
                        "workspaceId": "ws-x",
                        "workspaceName": "Alpha Build",
                    },
                    {
                        "id": "st-2",
                        "displayName": "Stage B",
                        "order": 1,
                        "workspaceId": "ws-y",
                        "workspaceName": "Beta Release",
                    },
                ]
            }
        ),
    )
    for stage, item in (("st-1", "nb-1"), ("st-2", "nb-2")):
        router.add(
            "GET",
            f"{dp}/dp-1/stages/{stage}/items",
            respond(body={"value": [{"itemId": item, "itemDisplayName": "transform", "itemType": "Notebook"}]}),
        )


async def stage_environments(contracts: EstateContracts) -> list[tuple[str, Environment]]:
    router = estate_router()
    deployment_routes(router)
    fabric = client_for(contracts, router=router)
    try:
        states = await fabric.deployments.get_deployment_states("nb-1")
    finally:
        await fabric.aclose()
    return [(s.item_id, s.environment) for s in states]


async def test_deployment_stages_use_the_position_when_nothing_is_declared() -> None:
    assert await stage_environments(EstateContracts()) == [("nb-1", DEV), ("nb-2", PROD)]


async def test_deployment_stages_follow_a_declared_workspace_environment() -> None:
    by_id = contracts_with(EnvironmentDeclaration(workspace_id="ws-x", environment=TEST))
    assert await stage_environments(by_id) == [("nb-1", TEST), ("nb-2", PROD)]
    by_pattern = contracts_with(EnvironmentDeclaration(workspace_name_pattern="Beta *", environment=TEST))
    assert await stage_environments(by_pattern) == [("nb-1", DEV), ("nb-2", TEST)]
    draft = contracts_with(EnvironmentDeclaration(workspace_id="ws-x", environment=TEST, inferred=True))
    assert await stage_environments(draft) == [("nb-1", DEV), ("nb-2", PROD)]  # a draft is not enforced
