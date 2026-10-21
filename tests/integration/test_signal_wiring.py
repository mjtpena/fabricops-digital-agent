"""How the settings choose the collectors, how Fabric job types map to items, and the two job tools."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from orqentis.bootstrap import Container, build_container
from orqentis.config.settings import RefreshSource
from orqentis.contracts import EstateContracts
from orqentis.domain.common import ItemType
from orqentis.domain.principals import SYSTEM_PRINCIPAL
from orqentis.domain.runs import CapacitySnapshot
from orqentis.fabric.rest.capacity_metrics import CapacityMetricsReader
from orqentis.fabric.rest.client import RestFabricClient
from orqentis.tools.models import ToolStatus
from orqentis.tools.registry import ToolContext
from tests.conftest import make_settings
from tests.integration.test_fabric_rest_client import FakeTokenProvider, Router, SleepRecorder, respond

NOW = datetime(2026, 10, 20, 2, 0, tzinfo=UTC)


async def collectors(tmp_path: Path, **overrides: Any) -> list[str]:
    container = await build_container(make_settings(tmp_path, **overrides), reset=True)
    try:
        return [c.name for c in container.ingestor._collectors]
    finally:
        await container.aclose()


async def test_the_default_collectors_in_mock_mode(tmp_path: Path) -> None:
    names = await collectors(tmp_path)
    assert names == [
        "pipeline-runs",  # first: its activity failures are stored before the failures they caused
        "notebook-runs",
        "spark-job-runs",
        "dataflow-runs",
        "semantic-model-refreshes",  # mock mode replays a Power BI shaped history
        "delta-schema-changes",
        "capacity-metrics",
        "static-events",
    ]


async def test_each_signal_source_can_be_switched_off(tmp_path: Path) -> None:
    names = await collectors(tmp_path, collect_item_jobs=False, collect_schema_changes=False)
    assert names == ["pipeline-runs", "semantic-model-refreshes", "capacity-metrics", "static-events"]


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (RefreshSource.WORKSPACE_MONITORING, ["workspace-monitoring-refreshes"]),
        (RefreshSource.POWERBI_API, ["semantic-model-refreshes"]),
        (RefreshSource.BOTH, ["semantic-model-refreshes", "workspace-monitoring-refreshes"]),
        (RefreshSource.NONE, []),
    ],
)
async def test_the_refresh_source_chooses_the_refresh_collectors(
    tmp_path: Path, source: RefreshSource, expected: list[str]
) -> None:
    names = await collectors(tmp_path, refresh_source=source)
    assert [n for n in names if "refresh" in n] == expected


@pytest.fixture
async def notebook_container(tmp_path: Path) -> AsyncIterator[Container]:
    container = await build_container(make_settings(tmp_path, scenario="notebook_failure"), reset=True)
    try:
        yield container
    finally:
        await container.aclose()


def ctx() -> ToolContext:
    return ToolContext(principal=SYSTEM_PRINCIPAL, trace_id="t")


async def test_the_job_tools_read_one_run_and_the_recent_runs_of_any_job_item(notebook_container: Container) -> None:
    one = await notebook_container.registry.invoke(
        "get_job_instance", {"item_id": "nb-inventory-snapshot", "run_id": "run-nb-inv-20261005"}, ctx()
    )
    assert one.status is ToolStatus.SUCCESS and one.data["status"] == "Failed"
    assert one.metadata["summary"].startswith("Job run-nb-inv-20261005 of inventory_snapshot Failed")
    many = await notebook_container.registry.invoke(
        "get_job_instances", {"item_id": "nb-inventory-snapshot", "limit": 3}, ctx()
    )
    assert many.status is ToolStatus.SUCCESS and next(r["id"] for r in many.data) == "run-nb-inv-20261005"
    assert len(many.data) == 3 and "1 failure(s)" in many.metadata["summary"]
    assert (
        await notebook_container.registry.invoke("get_job_instance", {"item_id": "x", "run_id": "y"}, ctx())
    ).status is not ToolStatus.SUCCESS
    bad = await notebook_container.registry.invoke("get_job_instances", {"item_id": "../etc", "limit": 3}, ctx())
    assert bad.status is not ToolStatus.SUCCESS  # item ids are validated like every other tool input


@pytest.mark.parametrize(
    ("source", "expected_source"),
    [
        (RefreshSource.WORKSPACE_MONITORING, "fabric.workspace-monitoring"),
        (RefreshSource.POWERBI_API, "powerbi.refreshes"),
    ],
)
async def test_the_refresh_history_tool_reads_the_configured_source(
    tmp_path: Path, source: RefreshSource, expected_source: str
) -> None:
    container = await build_container(
        make_settings(tmp_path, scenario="refresh_failure", refresh_source=source), reset=True
    )
    try:
        response = await container.registry.invoke(
            "get_semantic_model_refresh_history", {"semantic_model_id": "sm-marketing", "top": 5}, ctx()
        )
        assert response.status is ToolStatus.SUCCESS and response.source == expected_source
        assert next(r["status"] for r in response.data) == "Failed" and len(response.data) == 4
    finally:
        await container.aclose()


# ------------------------------------------------------------------------------------------------ REST client
def rest_client(router: Router, **kwargs: Any) -> RestFabricClient:
    return RestFabricClient(
        fabric_base_url="https://api.fabric.microsoft.com/v1",
        powerbi_base_url="https://api.powerbi.com/v1.0/myorg",
        token_provider=FakeTokenProvider(),
        contracts=EstateContracts(),
        tenant_id="t",
        transport=httpx.MockTransport(router),
        sleep=SleepRecorder(),
        **kwargs,
    )


@pytest.mark.parametrize(
    ("job_type", "item_type"),
    [
        ("Pipeline", ItemType.DATA_PIPELINE),
        ("RunNotebook", ItemType.NOTEBOOK),
        ("runnotebook", ItemType.NOTEBOOK),
        ("sparkjob", ItemType.SPARK_JOB_DEFINITION),
        ("SparkJob", ItemType.SPARK_JOB_DEFINITION),
        ("Refresh", ItemType.DATAFLOW),
        ("TableMaintenance", ItemType.OTHER),
    ],
)
async def test_fabric_job_types_map_to_item_types(job_type: str, item_type: ItemType) -> None:
    router = Router()
    run = {"id": "r1", "itemId": "i1", "jobType": job_type, "status": "Failed", "startTimeUtc": "2026-10-20T01:00:00Z"}
    router.add("GET", "/v1/workspaces/w1/items/i1/jobs/instances", respond(body={"value": [run]}))
    client = rest_client(router)
    try:
        (found,) = await client.jobs.list_job_instances("w1", "i1")
        assert found.item_type is item_type
    finally:
        await client.aclose()


async def test_spark_job_definitions_and_dataflows_are_typed_items() -> None:
    router = Router()
    items = [
        {"id": "s1", "type": "SparkJobDefinition", "displayName": "etl", "workspaceId": "w1"},
        {"id": "d1", "type": "Dataflow", "displayName": "dim", "workspaceId": "w1"},
    ]
    router.add("GET", "/v1/workspaces/w1", respond(body={"id": "w1", "displayName": "Sales [PROD]"}))
    router.add("GET", "/v1/workspaces/w1/items", respond(body={"value": items}))
    client = rest_client(router)
    try:
        found = {i.id: i.item_type for i in await client.items.list_items("w1")}
        assert found == {"s1": ItemType.SPARK_JOB_DEFINITION, "d1": ItemType.DATAFLOW}
    finally:
        await client.aclose()


async def test_capacity_metrics_go_through_the_reader_only_when_the_app_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def fake(
        self: CapacityMetricsReader, capacity_id: str, *, start: datetime, end: datetime
    ) -> list[CapacitySnapshot]:
        calls.append(capacity_id)
        return [CapacitySnapshot(capacity_id=capacity_id, timestamp=end, cu_utilization_percent=50.0)]

    monkeypatch.setattr(CapacityMetricsReader, "get_capacity_metrics", fake)
    configured = rest_client(
        Router(),
        capacity_metrics_workspace_id="11111111-1111-4111-8111-111111111111",
        capacity_metrics_dataset_id="22222222-2222-4222-8222-222222222222",
    )
    plain = rest_client(Router())
    try:
        snapshots = await configured.capacities.get_capacity_metrics("c1", start=NOW - timedelta(hours=1), end=NOW)
        assert [s.cu_utilization_percent for s in snapshots] == [50.0] and calls == ["c1"]
        from orqentis.fabric.errors import FabricCapabilityUnavailableError

        with pytest.raises(FabricCapabilityUnavailableError, match="ORQENTIS_CAPACITY_METRICS_WORKSPACE_ID"):
            await plain.capacities.get_capacity_metrics("c1", start=NOW - timedelta(hours=1), end=NOW)
    finally:
        await configured.aclose()
        await plain.aclose()
