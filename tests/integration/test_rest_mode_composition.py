"""REST mode end to end: the read-only Fabric REST adapter plugged into the full system.

Fabric/Power BI responses are mocked at the HTTP transport; everything above the transport
(catalog, collectors, detection, workflow, tools, policy, report) is the production code path.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from orqentis.bootstrap import build_container
from orqentis.config.clock import FixedClock
from orqentis.config.settings import FabricMode, Settings
from orqentis.domain.confidence import ConfidenceLevel
from orqentis.domain.incidents import IncidentStatus
from orqentis.fabric.fixtures import EstateContracts
from orqentis.fabric.rest.auth import FABRIC_SCOPE
from orqentis.fabric.rest.client import RestFabricClient
from tests.conftest import OPERATOR, REPO
from tests.integration.test_fabric_rest_client import (
    FABRIC,
    FABRIC_PATH,
    POWERBI,
    POWERBI_PATH,
    TOKENS,
    FakeTokenProvider,
    Router,
    SleepRecorder,
    respond,
)

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
ERROR = 'Notebook execution failed: AnalysisException: expected column "customer_id" is missing from input schema.'


def estate_routes(router: Router) -> None:
    ws = f"{FABRIC_PATH}/workspaces/ws-1"
    router.add(
        "GET",
        f"{FABRIC_PATH}/capacities",
        respond(
            body={
                "value": [
                    {
                        "id": "cap-1",
                        "displayName": "Prod F64",
                        "sku": "F64",
                        "region": "australiaeast",
                        "state": "Active",
                    }
                ]
            }
        ),
    )
    workspace = {"id": "ws-1", "displayName": "Sales [PROD]", "capacityId": "cap-1", "type": "Workspace"}
    router.add("GET", f"{FABRIC_PATH}/workspaces", respond(body={"value": [workspace]}))
    router.add("GET", ws, respond(body=workspace))
    items = [
        {"id": "pl-1", "displayName": "ingest_sales", "type": "DataPipeline", "workspaceId": "ws-1"},
        {"id": "nb-1", "displayName": "transform_sales", "type": "Notebook", "workspaceId": "ws-1"},
        {"id": "lh-1", "displayName": "SalesGold", "type": "Lakehouse", "workspaceId": "ws-1"},
        {"id": "sm-1", "displayName": "SalesModel", "type": "SemanticModel", "workspaceId": "ws-1"},
    ]
    router.add("GET", f"{ws}/items", respond(body={"value": items}))
    for item in items:
        router.add("GET", f"{ws}/items/{item['id']}", respond(body=item))
    job = {
        "id": "job-9",
        "itemId": "pl-1",
        "jobType": "Pipeline",
        "invokeType": "Scheduled",
        "status": "Failed",
        "rootActivityId": "root-9",
        "startTimeUtc": "2026-10-09T11:00:00Z",
        "endTimeUtc": "2026-10-09T11:02:00Z",
        "failureReason": {"errorCode": "ActivityFailed", "message": ERROR, "requestId": "req-1"},
    }
    router.add("GET", f"{ws}/items/pl-1/jobs/instances", respond(body={"value": [job]}))
    router.add("GET", f"{ws}/items/pl-1/jobs/instances/job-9", respond(body=job))
    router.add(
        "POST",
        f"{ws}/datapipelines/pipelineruns/job-9/queryactivityruns",
        respond(
            body={
                "value": [
                    {
                        "activityName": "transform_sales",
                        "activityType": "TridentNotebook",
                        "activityRunId": "act-2",
                        "status": "Failed",
                        "activityRunStart": "2026-10-09T11:00:30Z",
                        "activityRunEnd": "2026-10-09T11:01:59Z",
                        "input": {"notebookId": "nb-1"},
                        "error": {"errorCode": "2011", "message": ERROR, "failureType": "UserError"},
                    },
                ]
            }
        ),
    )
    router.add("GET", f"{POWERBI_PATH}/groups/ws-1/datasets/sm-1/refreshes", respond(body={"value": []}))
    router.add("GET", f"{FABRIC_PATH}/deploymentPipelines", respond(body={"value": []}))


def contracts_file(tmp_path: Path) -> Path:
    contracts = {
        "dependencies": [
            {
                "id": "d1",
                "tenant_id": "tenant-test",
                "source_item_id": "pl-1",
                "target_item_id": "nb-1",
                "dependency_type": "TRIGGERS",
            },
            {
                "id": "d2",
                "tenant_id": "tenant-test",
                "source_item_id": "nb-1",
                "target_item_id": "lh-1",
                "dependency_type": "WRITES_TO",
            },
            {
                "id": "d3",
                "tenant_id": "tenant-test",
                "source_item_id": "sm-1",
                "target_item_id": "lh-1",
                "dependency_type": "READS_FROM",
            },
        ]
    }
    path = tmp_path / "contracts.json"
    path.write_text(json.dumps(contracts))
    return path


async def test_rest_mode_detects_and_investigates_read_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    router = Router()
    estate_routes(router)
    tokens = FakeTokenProvider()

    def from_settings(
        cls: type[RestFabricClient], settings: Settings, contracts: EstateContracts, token_provider: object
    ) -> RestFabricClient:
        return RestFabricClient(
            fabric_base_url=FABRIC,
            powerbi_base_url=POWERBI,
            token_provider=tokens,
            contracts=contracts,
            tenant_id=settings.tenant_id,
            transport=httpx.MockTransport(router),
            sleep=SleepRecorder(),
            clock=FixedClock(NOW),
        )

    monkeypatch.setattr(RestFabricClient, "from_settings", classmethod(from_settings))
    settings = Settings(
        _env_file=None,
        fabric_mode=FabricMode.REST,
        tenant_id="tenant-test",  # type: ignore[call-arg]
        contracts_path=contracts_file(tmp_path),
        data_dir=tmp_path / "var",
        runbooks_dir=REPO / "runbooks",
        fixtures_dir=REPO / "fixtures",
    )
    container = await build_container(settings, reset=True, clock=FixedClock(NOW))
    try:
        assert set(container.catalog.items) == {"pl-1", "nb-1", "lh-1", "sm-1"}
        _, created = await container.collect_and_detect()
        assert [i.incident_id for i in created] == ["INC-2026-0001"]
        incident, trace = await container.investigations.investigate("INC-2026-0001", actor=OPERATOR)
    finally:
        await container.aclose()

    assert incident.status is IncidentStatus.AWAITING_APPROVAL
    assert incident.category == "SCHEMA_DRIFT"
    assert {a.display_name for a in incident.affected_assets} == {"SalesGold", "SalesModel"}
    # Capacity utilisation has no public API: the tool degrades, the investigation continues and says so.
    assert any("get_capacity_context" in e and "CAPABILITY_UNAVAILABLE" in e for e in trace.errors)
    # Without schema-change telemetry or history the diagnosis is honest about its weaker support.
    assert "not confirmed" in incident.probable_root_causes[0].statement
    assert incident.confidence is not None and incident.confidence.level is not ConfidenceLevel.HIGH
    # Strictly read-only against Fabric: the only non-GET call is the activity-run query (a read).
    writes = [r for r in router.requests if r.method != "GET"]
    assert writes and all(r.url.path.endswith("/queryactivityruns") for r in writes)
    assert all(r.headers["Authorization"].startswith("Bearer ") for r in router.requests)
    assert FABRIC_SCOPE in tokens.scopes
    assert TOKENS[FABRIC_SCOPE] not in incident.model_dump_json()
