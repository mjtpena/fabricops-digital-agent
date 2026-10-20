"""RestFabricClient replayed against responses recorded from the live FabricOps sandbox.

The fixtures in ``tests/fixtures/fabric_rest`` are what Microsoft Fabric and Power BI actually returned to the
Viewer-only test principal (see ``tests/live/record_fixtures.py``), with every identifier remapped to a synthetic
GUID and nothing else changed. Replaying them proves the adapter against real payloads (7-digit timestamps without a
``Z``, all-empty ``error`` objects on success, ``TimedOut`` activities with no message, the nested notebook error text,
...) without a tenant, credentials or network. The live counterparts are in ``tests/live``.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from orqentis.config.clock import FixedClock
from orqentis.domain.common import Environment, ItemType
from orqentis.domain.estate import GenericItem, Lakehouse, Notebook, Pipeline, Report, SemanticModel
from orqentis.domain.runs import RunStatus
from orqentis.domain.taxonomy import IncidentCategory
from orqentis.fabric.errors import FabricAuthError, FabricNotFoundError
from orqentis.fabric.fixtures import EstateContracts
from orqentis.fabric.rest.client import RestFabricClient, parse_datetime
from orqentis.fabric.rest.http import strip_privacy_tags
from orqentis.incidents.classification import ErrorClassifier
from tests.live.record_fixtures import FIXTURE_DIR

TENANT = "tenant-recorded"


# ------------------------------------------------------------------------------------------ harness
def load(label: str) -> dict[str, Any]:
    document: dict[str, Any] = json.loads((FIXTURE_DIR / f"{label}.json").read_text(encoding="utf-8"))
    return document


def entries(label: str) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = load(label)["entries"]
    return found


def response_json(label: str, index: int = 0) -> Any:
    return entries(label)[index]["response"]["json"]


class Replay(httpx.MockTransport):
    """Serves recorded (method, path) -> response pairs; anything not recorded is a test failure."""

    def __init__(self, *labels: str) -> None:
        self.table: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for label in labels:
            for entry in entries(label):
                req = entry["request"]
                self.table.setdefault((req["method"], req["path"]), []).append(entry)
        self.requests: list[httpx.Request] = []
        super().__init__(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        candidates = self.table.get((request.method, request.url.path))
        if not candidates:
            raise AssertionError(f"request not recorded: {request.method} {request.url.path}")
        entry = candidates.pop(0) if len(candidates) > 1 else candidates[0]
        reply = entry["response"]
        return httpx.Response(reply["status"], json=reply["json"], headers=reply.get("headers") or {})


class FakeTokens:
    async def get_token(self, scope: str) -> str:
        return "token-not-recorded"

    async def close(self) -> None:
        return None


async def _no_sleep(delay: float) -> None:
    return None


def replay_client(*labels: str) -> tuple[RestFabricClient, Replay]:
    replay = Replay(*labels)
    client = RestFabricClient(
        fabric_base_url="https://api.fabric.microsoft.com/v1",
        powerbi_base_url="https://api.powerbi.com/v1.0/myorg",
        token_provider=FakeTokens(),
        contracts=EstateContracts(),
        tenant_id=TENANT,
        max_retries=0,
        transport=replay,
        sleep=_no_sleep,
        clock=FixedClock(parse_datetime("2026-10-20T02:00:00Z")),  # type: ignore[arg-type]
    )
    return client, replay


@pytest.fixture
async def make_client() -> AsyncIterator[Callable[..., tuple[RestFabricClient, Replay]]]:
    created: list[RestFabricClient] = []

    def factory(*labels: str) -> tuple[RestFabricClient, Replay]:
        client, replay = replay_client(*labels)
        created.append(client)
        return client, replay

    yield factory
    for client in created:
        await client.aclose()


def labels_matching(prefix: str) -> list[str]:
    return sorted(p.stem for p in FIXTURE_DIR.glob(f"{prefix}*.json"))


GUID_IN_PATH = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
Factory = Callable[..., tuple[RestFabricClient, Replay]]

DEV, TEST_ENV, PROD = Environment.DEV, Environment.TEST, Environment.PROD
JOB_STATUS = {"Completed": RunStatus.SUCCEEDED, "Failed": RunStatus.FAILED}
ACTIVITY_STATUS = {"Succeeded": RunStatus.SUCCEEDED, "Failed": RunStatus.FAILED, "TimedOut": RunStatus.TIMED_OUT}


def path_ids(label: str, index: int = 0) -> list[str]:
    return GUID_IN_PATH.findall(entries(label)[index]["request"]["path"])


# --------------------------------------------------------------------------------- workspaces, items
async def test_recorded_workspaces_map_environment_capacity_and_type(make_client: Factory) -> None:
    client, _ = make_client("workspaces")
    workspaces = await client.workspaces.list_workspaces()
    raw = response_json("workspaces")["value"]

    assert [w.id for w in workspaces] == [r["id"] for r in raw]
    assert {w.display_name: w.environment for w in workspaces} == {
        "FabricOps Sandbox [DEV]": Environment.DEV,
        "FabricOps Sandbox [TEST]": Environment.TEST,
        "FabricOps Sandbox [PROD]": Environment.PROD,
        "FabricOps Sandbox Consumer": Environment.UNKNOWN,
    }
    assert {w.capacity_id for w in workspaces} == {raw[0]["capacityId"]}
    assert all(w.tenant_id == TENANT and w.metadata == {"workspace_type": "Workspace"} for w in workspaces)


async def test_recorded_items_keep_the_sql_endpoint_as_a_generic_item(make_client: Factory) -> None:
    client, _ = make_client("workspaces", "items_dev")
    dev = next(w for w in await client.workspaces.list_workspaces() if w.environment is Environment.DEV)
    items = await client.items.list_items(dev.id)
    kinds = {(type(i), i.display_name) for i in items}

    assert kinds == {
        (Lakehouse, "crm_source"),
        (GenericItem, "crm_source"),  # the lakehouse's SQL analytics endpoint, same name, different id
        (Notebook, "seed_customers"),
        (Notebook, "transform_customers"),
        (Pipeline, "ingest_customers"),
    }
    endpoint = next(i for i in items if isinstance(i, GenericItem))
    assert endpoint.fabric_type == "SQLEndpoint" and endpoint.item_type is ItemType.OTHER
    assert len({i.id for i in items}) == len(items)
    assert {i.environment for i in items} == {Environment.DEV} and {i.workspace_id for i in items} == {dev.id}
    filtered = await client.items.list_items(dev.id, item_type=ItemType.OTHER)
    assert [i.id for i in filtered] == [endpoint.id]  # OTHER is filtered client-side


async def test_recorded_consumer_items_are_a_semantic_model_and_a_report(make_client: Factory) -> None:
    client, _ = make_client("workspaces", "items_consumer")
    consumer = next(w for w in await client.workspaces.list_workspaces() if w.display_name.endswith("Consumer"))
    items = await client.items.list_items(consumer.id)
    assert {(type(i), i.display_name) for i in items} == {
        (SemanticModel, "customers_model"),
        (Report, "customers_report"),
    }


# ------------------------------------------------------------------------------------- job instances
async def test_recorded_job_instances_are_newest_first_with_failures_mapped(make_client: Factory) -> None:
    client, _ = make_client("workspaces", "job_instances_test_pipeline")
    await client.workspaces.list_workspaces()  # the environment is learned from the workspace listing
    entry = entries("job_instances_test_pipeline")[1]
    workspace_id, pipeline_id = path_ids("job_instances_test_pipeline", 1)
    raw = entry["response"]["json"]["value"]

    runs = await client.jobs.list_job_instances(workspace_id, pipeline_id)

    assert [r.id for r in runs] == [r["id"] for r in raw]
    starts = [r["startTimeUtc"] for r in raw]
    assert starts == sorted(starts, reverse=True), "the recorded service response is newest-first"
    assert [r.start_time for r in runs] == sorted((r.start_time for r in runs), reverse=True)  # type: ignore[type-var]
    assert [r.status for r in runs] == [JOB_STATUS[r["status"]] for r in raw]
    assert all(r.environment is Environment.TEST and r.item_type is ItemType.DATA_PIPELINE for r in runs)
    assert all(r.start_time and r.start_time.tzinfo is not None for r in runs)  # naive, 7-digit UTC stamps
    first = runs[0]
    assert first.start_time == datetime(2026, 10, 20, 1, 14, 48, 453333, tzinfo=UTC)
    assert first.correlation_id == raw[0]["rootActivityId"] and first.invoke_type == "Manual"
    assert first.failure is not None and first.failure.error_code == "2108"
    assert first.failure.message.startswith("Operation on target call_crm_source failed")
    assert {r.failure.error_code for r in runs if r.failure} == {"2108", "2015", "500", "2451"}
    assert all(r.failure is None for r in runs if r.status is RunStatus.SUCCEEDED)  # failureReason: null


async def test_recorded_limit_keeps_the_newest_runs(make_client: Factory) -> None:
    client, _ = make_client("workspaces", "job_instances_test_pipeline")
    await client.workspaces.list_workspaces()
    workspace_id, pipeline_id = path_ids("job_instances_test_pipeline", 1)
    raw = entries("job_instances_test_pipeline")[1]["response"]["json"]["value"]
    runs = await client.jobs.list_job_instances(workspace_id, pipeline_id, limit=2)
    assert [r.id for r in runs] == [r["id"] for r in raw[:2]]


# ------------------------------------------------------------------------------------- activity runs
@pytest.mark.parametrize("label", labels_matching("activity_runs_"))
async def test_recorded_activity_runs_map_every_field(label: str, make_client: Factory) -> None:
    client, replay = make_client(label)
    workspace_id, pipeline_id, run_id = path_ids(label, 0)
    raw = entries(label)[1]["response"]["json"]["value"]

    activities = await client.pipelines.query_activity_runs(workspace_id, pipeline_id, run_id)

    sent = json.loads(replay.requests[-1].content)
    assert sent == entries(label)[1]["request"]["json"]  # exactly the query Fabric accepted
    assert len(activities) == len(raw) > 0
    for activity, source in zip(activities, raw, strict=True):
        assert activity.id == source["activityRunId"] and activity.pipeline_run_id == run_id
        assert activity.activity_name == source["activityName"] and activity.activity_type == source["activityType"]
        assert activity.status is ACTIVITY_STATUS[source["status"]]
        assert activity.start_time and activity.end_time and activity.start_time.tzinfo is not None
        assert activity.target_item_id == source["input"].get("notebookId")
        message = source["error"]["message"]
        if message:
            assert activity.error is not None and activity.error.message == message.strip()
            assert activity.error.error_code == (source["error"]["errorCode"] or None)
        elif source["status"] == "TimedOut":
            assert activity.error is not None and "timed out" in activity.error.message
        else:
            assert activity.error is None  # Fabric sends an all-empty error object on success


async def test_recorded_timed_out_activity_is_classifiable_as_a_timeout(make_client: Factory) -> None:
    """Regression: Fabric reports a timed-out activity with status TimedOut and an EMPTY error object, and the
    pipeline's own message is a generic 500. Without a synthesised message the timeout was invisible."""
    label = next(lb for lb in labels_matching("activity_runs_") if "TimedOut" in json.dumps(entries(lb)[1]))
    client, _ = make_client(label)
    (activity,) = await client.pipelines.query_activity_runs(*path_ids(label, 0))

    assert activity.status is RunStatus.TIMED_OUT and activity.status.is_failure
    assert activity.error is not None and activity.error.error_code == "ActivityTimedOut"
    assert activity.error.failure_type == "UserError"  # kept from Fabric's empty error object
    match = ErrorClassifier().classify(activity.error.message)
    assert match is not None and match.category == IncidentCategory.PIPELINE_TIMEOUT


async def test_recorded_notebook_failure_keeps_the_nested_exception_text(make_client: Factory) -> None:
    label = next(
        lb for lb in labels_matching("activity_runs_") if "Notebook execution failed" in json.dumps(entries(lb)[1])
    )
    client, _ = make_client(label)
    (activity,) = await client.pipelines.query_activity_runs(*path_ids(label, 0))

    assert activity.status is RunStatus.FAILED and activity.activity_type == "TridentNotebook"
    assert activity.error is not None and activity.error.error_code == "2451"
    assert "RuntimeError" in activity.error.message and "raised on purpose" in activity.error.message
    assert activity.target_item_id is not None and activity.output["status"] == "Failed"


async def test_recorded_entra_error_from_a_web_activity_is_classified_as_authentication(make_client: Factory) -> None:
    label = next(lb for lb in labels_matching("activity_runs_") if "AADSTS" in json.dumps(entries(lb)[1]))
    client, _ = make_client(label)
    (activity,) = await client.pipelines.query_activity_runs(*path_ids(label, 0))

    assert activity.activity_type == "WebActivity" and activity.target_item_id is None
    assert activity.error is not None and "AADSTS700016" in activity.error.message
    match = ErrorClassifier().classify(activity.error.message)
    assert match is not None and match.category == IncidentCategory.AUTH_FAILURE


async def test_recorded_successful_activity_has_no_error(make_client: Factory) -> None:
    label = next(lb for lb in labels_matching("activity_runs_") if "Succeeded" in json.dumps(entries(lb)[1]))
    client, _ = make_client(label)
    (activity,) = await client.pipelines.query_activity_runs(*path_ids(label, 0))
    assert activity.status is RunStatus.SUCCEEDED and activity.error is None
    assert activity.duration_seconds is not None and activity.duration_seconds > 1


# ------------------------------------------------------------------------------------------ refreshes
async def test_recorded_refresh_history_maps_statuses_types_and_failures(make_client: Factory) -> None:
    """Captured as the sandbox owner: a Viewer-only principal cannot read refresh history at all."""
    client, _ = make_client("refreshes_consumer_model")
    workspace_id, model_id = path_ids("refreshes_consumer_model", 0)
    raw = response_json("refreshes_consumer_model")["value"]

    refreshes = await client.semantic_models.get_refresh_history(workspace_id, model_id, top=20)

    assert [r.id for r in refreshes] == [r["requestId"] for r in raw]
    assert [r.start_time for r in refreshes] == sorted((r.start_time for r in refreshes), reverse=True)  # type: ignore[type-var]
    assert [r.status for r in refreshes] == [JOB_STATUS[r["status"]] for r in raw]
    assert {r.refresh_type for r in refreshes} == {"ViaEnhancedApi", "DirectLakeFraming"}
    failed = next(r for r in refreshes if r.status is RunStatus.FAILED)
    assert failed.error is not None and failed.error.error_code == "ModelRefresh_ShortMessage_ProcessingError"
    assert "customers_clean_removed" in failed.error.message
    assert "<oii>" not in failed.error.message and "</oii>" not in failed.error.message  # privacy markers dropped
    assert all(r.error is None for r in refreshes if r.status is RunStatus.SUCCEEDED)
    assert failed.metadata["extended_status"] == "Failed" and failed.metadata["refresh_attempts"] == 1


# ---------------------------------------------------------------------------------------- errors
async def test_recorded_refresh_history_403_for_viewer_maps_to_auth_error(make_client: Factory) -> None:
    label = "error_refresh_history_forbidden"
    client, _ = make_client(label)
    with pytest.raises(FabricAuthError) as caught:
        await client.semantic_models.get_refresh_history(*path_ids(label), top=20)
    error = caught.value
    assert error.status_code == 403 and error.error_code == "Unauthorized" and not error.retryable
    assert error.request_id == response_json(label)["error"].get("requestId", error.request_id)
    assert "<eupi>" not in error.message and "insufficient privileges" in error.message


def assert_not_found(error: FabricNotFoundError, code: str) -> None:
    assert error.status_code == 404 and error.error_code == code and not error.retryable
    assert error.request_id and error.correlation_id


async def test_recorded_missing_item_maps_to_not_found(make_client: Factory) -> None:
    client, _ = make_client("error_item_not_found")
    with pytest.raises(FabricNotFoundError) as caught:
        await client.items.get_item(*path_ids("error_item_not_found"))
    assert_not_found(caught.value, "EntityNotFound")


async def test_recorded_missing_workspace_maps_to_not_found(make_client: Factory) -> None:
    client, _ = make_client("error_workspace_not_found")
    with pytest.raises(FabricNotFoundError) as caught:
        await client.workspaces.get_workspace(*path_ids("error_workspace_not_found"))
    assert_not_found(caught.value, "WorkspaceNotFound")


async def test_recorded_job_instances_of_a_semantic_model_map_to_not_found(make_client: Factory) -> None:
    client, _ = make_client("error_model_job_instances_not_found")
    with pytest.raises(FabricNotFoundError) as caught:
        await client.jobs.list_job_instances(*path_ids("error_model_job_instances_not_found"))
    assert_not_found(caught.value, "ItemNotFound")


# ---------------------------------------------------------------- capacities and deployment pipelines
async def test_recorded_capacities_are_only_those_open_to_the_principal(make_client: Factory) -> None:
    client, _ = make_client("capacities")
    capacities = await client.capacities.list_capacities()
    raw = response_json("capacities")["value"]
    assert [c.id for c in capacities] == [r["id"] for r in raw]
    assert all(c.sku and c.state == "Active" and c.region for c in capacities)
    assert all(c.environment is Environment.UNKNOWN for c in capacities)


async def test_recorded_deployment_pipelines_are_empty_for_a_viewer(make_client: Factory) -> None:
    client, _ = make_client("deployment_pipelines")
    assert await client.deployments.get_deployment_states("00000000-0000-4000-8000-0000000000ff") == []


# --------------------------------------------------------------------------------- privacy markers
def test_privacy_markers_are_dropped_but_their_content_is_kept() -> None:
    assert strip_privacy_tags("table '<oii>customers_clean</oii>' of user <eupi>abc</eupi>") == (
        "table 'customers_clean' of user abc"
    )
    assert strip_privacy_tags("a <b>bold</b> <ii>x</II>") == "a <b>bold</b> x"
    assert strip_privacy_tags("no markers here") == "no markers here"
