"""REST mode with every WS7 source: job instances, workspace monitoring (Kusto) and OneLake, over mocked HTTP.

Everything above the transport is the production code path (settings -> client -> collectors -> rules -> workflows). The
monitoring and OneLake responses are the documented-schema fixtures and hand-written Delta commits; no tenant is
contacted.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from orqentis.bootstrap import build_container
from orqentis.config.clock import FixedClock
from orqentis.config.settings import FabricMode, RefreshSource, Settings
from orqentis.contracts import EstateContracts
from orqentis.domain.events import EventType
from orqentis.fabric.rest.client import RestFabricClient
from orqentis.storage.event_store import EventQuery
from tests.conftest import OPERATOR, REPO
from tests.integration.test_fabric_rest_client import Router, SleepRecorder, respond
from tests.live.record_fixtures import FIXTURE_DIR

NOW = datetime(2026, 10, 20, 2, 0, tzinfo=UTC)
FABRIC = "https://api.fabric.example.invalid/v1"
WS = "00000000-0000-4000-8000-000000000080"
KQL = "00000000-0000-4000-8000-000000000081"
MODEL = "00000000-0000-4000-8000-000000000085"  # the model the monitoring fixture reports on
NOTEBOOK = "00000000-0000-4000-8000-0000000000e1"
LAKEHOUSE = "00000000-0000-4000-8000-0000000000e2"
PIPELINE = "00000000-0000-4000-8000-0000000000e3"
ONELAKE = "https://onelake.dfs.fabric.microsoft.com"

RUNTIME_ERROR = (
    "Notebook execution failed at Notebook service with http status code - '200', please check the Run logs on "
    'Notebook, additional details - \'{"errorCode":"200","errorName":"ValueError","errorValue":"bad unit_cost",'
    '"message":"Notebook execution is in Failed state, runId: x"}\' : '
)


class AnyScope:
    """A token provider for any scope, remembering which were asked for."""

    def __init__(self) -> None:
        self.scopes: list[str] = []

    async def get_token(self, scope: str) -> str:
        self.scopes.append(scope)
        return "token-not-real"

    async def close(self) -> None:
        return None


def fixture_response(label: str, index: int = 0) -> dict[str, object]:
    document = json.loads((FIXTURE_DIR / f"{label}.json").read_text(encoding="utf-8"))
    body: dict[str, object] = document["entries"][index]["response"]["json"]
    return body


def schema_line(columns: list[str]) -> bytes:
    fields = [{"name": c, "type": "string", "nullable": True, "metadata": {}} for c in columns]
    action = {"metaData": {"id": "t", "schemaString": json.dumps({"type": "struct", "fields": fields})}}
    return (
        json.dumps({"commitInfo": {"timestamp": 1_792_000_000_000, "operation": "WRITE"}})
        + "\n"
        + json.dumps(action)
        + "\n"
    ).encode()


def build_router() -> Router:
    router = Router()
    ws = f"/v1/workspaces/{WS}"
    router.add("GET", "/v1/capacities", respond(body={"value": []}))
    workspace = {"id": WS, "displayName": "Sales [PROD]", "type": "Workspace"}
    router.add("GET", "/v1/workspaces", respond(body={"value": [workspace]}))
    router.add("GET", ws, respond(body=workspace))
    items = [
        {"id": PIPELINE, "displayName": "ingest", "type": "DataPipeline", "workspaceId": WS},
        {"id": NOTEBOOK, "displayName": "transform", "type": "Notebook", "workspaceId": WS},
        {"id": LAKEHOUSE, "displayName": "bronze", "type": "Lakehouse", "workspaceId": WS},
        {"id": MODEL, "displayName": "customers_model", "type": "SemanticModel", "workspaceId": WS},
        {"id": KQL, "displayName": "Monitoring KQL database", "type": "KQLDatabase", "workspaceId": WS},
    ]
    router.add("GET", f"{ws}/items", respond(body={"value": items}))
    for item in items:
        router.add("GET", f"{ws}/items/{item['id']}", respond(body=item))
    run = {
        "id": "job-nb-9",
        "itemId": NOTEBOOK,
        "jobType": "RunNotebook",
        "invokeType": "Manual",
        "status": "Failed",
        "rootActivityId": "root-9",
        "startTimeUtc": "2026-10-20T01:30:00.0000000",
        "endTimeUtc": "2026-10-20T01:31:30.0000000",
        "failureReason": {"errorCode": "200", "message": RUNTIME_ERROR, "requestId": "req-1"},
    }
    router.add("GET", f"{ws}/items/{NOTEBOOK}/jobs/instances", respond(body={"value": [run]}))
    router.add("GET", f"{ws}/items/{NOTEBOOK}/jobs/instances/job-nb-9", respond(body=run))
    router.add("GET", f"{ws}/items/{PIPELINE}/jobs/instances", respond(body={"value": []}))
    router.add("GET", "/v1/deploymentPipelines", respond(body={"value": []}))
    # workspace monitoring: the documented-schema fixtures
    router.add("GET", f"{ws}/kqlDatabases", respond(body=fixture_response("doc_monitoring_01_discovery")))
    router.add("POST", "/v1/rest/query", respond(body=fixture_response("doc_monitoring_03_refreshes")))
    # OneLake: the log of lakehouse table 'customers' (version 1 renamed customer_id to client_id)
    log = f"/{WS}/{LAKEHOUSE}/Tables/customers/_delta_log"
    listing = {
        "paths": [
            {
                "name": f"{LAKEHOUSE}/Tables/customers/_delta_log/{v:020d}.json",
                "lastModified": "Tue, 20 Oct 2026 01:00:00 GMT",
            }
            for v in (0, 1)
        ]
    }
    router.add("GET", f"/{WS}", respond(body=listing))
    router.add(
        "GET", f"{log}/{0:020d}.json", lambda r: httpx.Response(200, content=schema_line(["customer_id", "name"]))
    )
    router.add("GET", f"{log}/{1:020d}.json", lambda r: httpx.Response(200, content=schema_line(["client_id", "name"])))
    return router


@pytest.fixture
def rest_settings(tmp_path: Path) -> Settings:
    contracts = {
        "dependencies": [
            {
                "id": "d1",
                "tenant_id": "t",
                "source_item_id": PIPELINE,
                "target_item_id": NOTEBOOK,
                "dependency_type": "TRIGGERS",
            },
            {
                "id": "d2",
                "tenant_id": "t",
                "source_item_id": NOTEBOOK,
                "target_item_id": LAKEHOUSE,
                "dependency_type": "READS_FROM",
            },
            {
                "id": "d3",
                "tenant_id": "t",
                "source_item_id": MODEL,
                "target_item_id": LAKEHOUSE,
                "dependency_type": "READS_FROM",
            },
        ],
        "delta_tables": [{"id": "tbl-customers", "item_id": LAKEHOUSE, "table": "customers"}],
    }
    path = tmp_path / "contracts.json"
    path.write_text(json.dumps(contracts), encoding="utf-8")
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        fabric_mode=FabricMode.REST,
        tenant_id="tenant-test",
        contracts_path=path,
        data_dir=tmp_path / "var",
        runbooks_dir=REPO / "runbooks",
        fixtures_dir=REPO / "fixtures",
    )


async def test_every_signal_source_works_over_http_and_stays_read_only(
    rest_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert rest_settings.refresh_source is RefreshSource.WORKSPACE_MONITORING  # the default in rest mode
    router, tokens = build_router(), AnyScope()

    def from_settings(
        cls: type[RestFabricClient], settings: Settings, contracts: EstateContracts, token_provider: object
    ) -> RestFabricClient:
        return RestFabricClient(
            fabric_base_url=FABRIC,
            powerbi_base_url="https://api.powerbi.example.invalid/v1.0/myorg",
            token_provider=tokens,
            contracts=contracts,
            tenant_id=settings.tenant_id,
            transport=httpx.MockTransport(router),
            sleep=SleepRecorder(),
            clock=FixedClock(NOW),
            onelake_base_url=settings.onelake_base_url,
            onelake_scope=settings.onelake_scope,
            kusto_scope=settings.kusto_scope,
            monitoring_database_name=settings.workspace_monitoring_database,
        )

    monkeypatch.setattr(RestFabricClient, "from_settings", classmethod(from_settings))
    container = await build_container(rest_settings, reset=True, clock=FixedClock(NOW))
    try:
        ingestion, created = await container.collect_and_detect()
        assert ingestion.errors == {} and ingestion.unavailable == {}
        assert {"notebook-runs", "workspace-monitoring-refreshes", "delta-schema-changes", "pipeline-runs"} <= {
            label.split(":")[0] for label in ingestion.windows
        }
        by_rule = {i.metadata["detection_rule"]: i for i in created}
        assert set(by_rule) == {"DET-NOTEBOOK-FAILED", "DET-REFRESH-FAILED"}

        schema_events = await container.event_store.query(EventQuery(event_types=[EventType.SCHEMA_CHANGED]))
        assert [(e.item_id, e.payload["removed_columns"], e.payload["added_columns"]) for e in schema_events] == [
            (LAKEHOUSE, ["customer_id"], ["client_id"])
        ]

        notebook, trace = await container.investigations.investigate(
            by_rule["DET-NOTEBOOK-FAILED"].incident_id, actor=OPERATOR
        )
        assert trace.workflow == "item-job-failure-v1" and notebook.category == "ACTIVITY_FAILURE"
        assert "ValueError" in notebook.probable_root_causes[0].statement

        refresh, trace = await container.investigations.investigate(
            by_rule["DET-REFRESH-FAILED"].incident_id, actor=OPERATOR
        )
        assert trace.workflow == "refresh-failure-v1"
        assert refresh.category == "AUTH_FAILURE"  # the engine said the source refused the model's credentials
        refresh_evidence = next(e for e in refresh.evidence if e.evidence_type.value == "SEMANTIC_MODEL_REFRESH")
        assert refresh_evidence.structured_payload["status"] == "Failed" and "<oii>" not in json.dumps(
            refresh.model_dump(mode="json")
        )
    finally:
        await container.aclose()

    # strictly read-only: GETs, plus the two read-only POSTs (activity-run query, Kusto query)
    posts = {r.url.path for r in router.requests if r.method != "GET"}
    assert posts <= {"/v1/rest/query"} or all(p.endswith("/queryactivityruns") or p == "/v1/rest/query" for p in posts)
    assert {r.method for r in router.requests} <= {"GET", "POST"}
    assert (
        "https://storage.azure.com/.default" in tokens.scopes
        and "https://kusto.kusto.windows.net/.default" in tokens.scopes
    )
    # the Power BI refresh history (dataset Write) is never called when workspace monitoring is the source
    assert not [r for r in router.requests if r.url.path.endswith("/refreshes")]
