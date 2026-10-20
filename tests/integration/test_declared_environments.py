"""Declared environments flow from the contracts file through the REST adapter into severity and policy."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from orqentis.config.clock import FixedClock
from orqentis.contracts import EstateContracts, load_contracts
from orqentis.domain.actions import Action, ActionType, PolicyEffect
from orqentis.domain.common import Environment
from orqentis.domain.taxonomy import Severity
from orqentis.fabric.rest.client import RestFabricClient
from orqentis.incidents.detection import PipelineFailureRule
from orqentis.policies.engine import PolicyEngine
from orqentis.telemetry.collectors import PipelineRunCollector
from orqentis.topology.catalog import EstateCatalog
from tests.integration.test_fabric_rest_client import FABRIC, FABRIC_PATH, POWERBI, FakeTokenProvider, Router, respond

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)

WORKSPACES = [
    {"id": "ws-a", "displayName": "Sales [DEV]", "capacityId": "cap-1"},  # the name lies: it is production
    {"id": "ws-b", "displayName": "Finance Reporting", "capacityId": "cap-1"},  # no environment in the name
    {"id": "ws-c", "displayName": "Marketing [TEST]", "capacityId": "cap-1"},  # undeclared: inferred
    {"id": "ws-d", "displayName": "Lab [PROD]", "capacityId": "cap-1"},
]
ITEMS = {
    "ws-a": [{"id": "pl-a", "displayName": "ingest_sales", "type": "DataPipeline"}],
    "ws-b": [{"id": "pl-b", "displayName": "ingest_finance", "type": "DataPipeline"}],
    "ws-c": [{"id": "pl-c", "displayName": "ingest_marketing", "type": "DataPipeline"}],
    "ws-d": [
        {"id": "pl-d", "displayName": "ingest_lab", "type": "DataPipeline"},
        {"id": "nb-lab", "displayName": "explore", "type": "Notebook"},
    ],
}


def estate_router() -> Router:
    router = Router()
    router.add("GET", f"{FABRIC_PATH}/capacities", respond(body={"value": []}))
    router.add("GET", f"{FABRIC_PATH}/workspaces", respond(body={"value": WORKSPACES}))
    for workspace_id, items in ITEMS.items():
        base = f"{FABRIC_PATH}/workspaces/{workspace_id}"
        router.add("GET", f"{base}/items", respond(body={"value": items}))
        for item in items:
            if item["type"] != "DataPipeline":
                continue
            failed = {
                "id": f"job-{item['id']}",
                "itemId": item["id"],
                "jobType": "Pipeline",
                "status": "Failed",
                "startTimeUtc": (NOW - timedelta(minutes=30)).isoformat(),
                "endTimeUtc": (NOW - timedelta(minutes=28)).isoformat(),
                "failureReason": {"errorCode": "ActivityFailed", "message": "boom"},
            }
            instances = f"{base}/items/{item['id']}/jobs/instances"
            router.add("GET", instances, respond(body={"value": [failed]}))
            router.add("GET", f"{instances}/{failed['id']}", respond(body=failed))
            runs = f"{base}/datapipelines/pipelineruns/{failed['id']}/queryactivityruns"
            router.add("POST", runs, respond(body={"value": []}))
    return router


def client_for(contracts: EstateContracts) -> RestFabricClient:
    return RestFabricClient(
        fabric_base_url=FABRIC,
        powerbi_base_url=POWERBI,
        token_provider=FakeTokenProvider(),
        contracts=contracts,
        tenant_id="tenant-test",
        transport=httpx.MockTransport(estate_router()),
        clock=FixedClock(NOW),
    )


DECLARATIONS = [
    {"workspace_id": "ws-a", "environment": "PROD", "note": "Sales is production despite its name"},
    {"workspace_name_pattern": "Finance *", "environment": "PROD"},
    {"item_id": "nb-lab", "environment": "DEV"},
]


def write_contracts(tmp_path: Path) -> Path:
    path = tmp_path / "contracts.json"
    path.write_text(json.dumps({"environments": DECLARATIONS}), encoding="utf-8")
    return path


async def collect_severities(fabric: RestFabricClient, catalog: EstateCatalog) -> dict[str, Severity]:
    events = await PipelineRunCollector(fabric, catalog).collect(since=NOW - timedelta(hours=1), until=NOW)
    rule = PipelineFailureRule()
    out: dict[str, Severity] = {}
    for event in events:
        candidate = rule.evaluate(event)
        if candidate is not None:
            out[candidate.primary_item_id] = candidate.severity
    return out


def change_permissions(catalog: EstateCatalog, item_id: str) -> Action:
    node = catalog.node(item_id)
    assert node is not None
    return Action(
        action_id="ACT-1",
        incident_id="INC-1",
        action_type=ActionType.CHANGE_PERMISSIONS,
        title="Change permissions",
        target_item_id=item_id,
        environment=node.environment,  # exactly how the mutation tools classify an action
        requested_by="agent:FabricOpsInvestigator",
        created_at=NOW,
    )


# --------------------------------------------------------------------------------------------- tests
async def test_declared_environments_override_name_inference(tmp_path: Path) -> None:
    contracts = load_contracts(write_contracts(tmp_path))
    fabric = client_for(contracts)
    try:
        catalog = await EstateCatalog.load(fabric, contracts)
    finally:
        await fabric.aclose()

    environments = {w.id: w.environment for w in catalog.workspaces.values()}
    assert environments == {
        "ws-a": Environment.PROD,  # declared by id; the name says DEV
        "ws-b": Environment.PROD,  # declared by pattern; the name says nothing
        "ws-c": Environment.TEST,  # undeclared: inferred from "[TEST]"
        "ws-d": Environment.PROD,  # undeclared: inferred from "[PROD]"
    }
    assert catalog.workspaces["ws-a"].metadata["environment_source"] == "declared_id"
    assert catalog.workspaces["ws-b"].metadata["environment_source"] == "declared_pattern"
    assert "environment_source" not in catalog.workspaces["ws-c"].metadata  # inferred: no declaration decided it
    # Items inherit their workspace's resolved environment unless an item declaration says otherwise.
    assert catalog.items["pl-a"].environment is Environment.PROD
    assert catalog.items["pl-b"].environment is Environment.PROD
    assert catalog.items["pl-c"].environment is Environment.TEST
    assert catalog.items["pl-d"].environment is Environment.PROD
    assert catalog.items["nb-lab"].environment is Environment.DEV  # declared by item id inside a PROD workspace


async def test_without_declarations_the_names_decide() -> None:
    fabric = client_for(EstateContracts())
    try:
        catalog = await EstateCatalog.load(fabric, EstateContracts())
    finally:
        await fabric.aclose()
    assert {w.id: w.environment for w in catalog.workspaces.values()} == {
        "ws-a": Environment.DEV,
        "ws-b": Environment.UNKNOWN,
        "ws-c": Environment.TEST,
        "ws-d": Environment.PROD,
    }


async def test_incident_severity_follows_the_resolved_environment(tmp_path: Path) -> None:
    contracts = load_contracts(write_contracts(tmp_path))
    fabric = client_for(contracts)
    try:
        catalog = await EstateCatalog.load(fabric, contracts)
        declared = await collect_severities(fabric, catalog)
    finally:
        await fabric.aclose()
    assert declared == {
        "pl-a": Severity.HIGH,  # declared PROD (the name says DEV)
        "pl-b": Severity.HIGH,  # declared PROD by pattern
        "pl-c": Severity.MEDIUM,  # inferred TEST
        "pl-d": Severity.HIGH,  # inferred PROD
    }

    plain = client_for(EstateContracts())
    try:
        inferred = await collect_severities(plain, await EstateCatalog.load(plain, EstateContracts()))
    finally:
        await plain.aclose()
    assert inferred["pl-a"] is Severity.LOW  # without the declaration the DEV name would under-rate a PROD pipeline
    assert inferred["pl-b"] is Severity.MEDIUM  # UNKNOWN


async def test_policy_pol_002_follows_the_resolved_environment(tmp_path: Path) -> None:
    policy = PolicyEngine.default(FixedClock(NOW))
    contracts = load_contracts(write_contracts(tmp_path))
    fabric = client_for(contracts)
    try:
        catalog = await EstateCatalog.load(fabric, contracts)
    finally:
        await fabric.aclose()

    declared = policy.evaluate(change_permissions(catalog, "pl-a"))
    assert declared.effect is PolicyEffect.DENY
    assert "POL-002-deny-production-permissions" in declared.matched_rule_ids

    plain = client_for(EstateContracts())
    try:
        plain_catalog = await EstateCatalog.load(plain, EstateContracts())
    finally:
        await plain.aclose()
    undeclared = policy.evaluate(change_permissions(plain_catalog, "pl-a"))  # the name says DEV
    assert undeclared.effect is PolicyEffect.REQUIRE_APPROVAL
    assert "POL-002-deny-production-permissions" not in undeclared.matched_rule_ids
    # No mutation is ever auto-allowed, whatever the environment.
    assert undeclared.effect is not PolicyEffect.ALLOW
