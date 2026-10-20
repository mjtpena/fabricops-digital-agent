"""RestFabricClient against the live sandbox, as the workspace-Viewer-only test principal.

Every assertion is also a statement about least privilege: nothing here may need more than Viewer.
The failed runs these tests read are left in the TEST pipeline's history by
``scripts/live-estate/seed-history.sh`` (Fabric keeps job history for 30 days).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from orqentis.domain.common import Environment, ItemType
from orqentis.domain.estate import GenericItem, Lakehouse, Notebook, Pipeline, Report, SemanticModel
from orqentis.domain.runs import ActivityRun, PipelineRun, RunStatus
from orqentis.fabric.errors import (
    FabricAuthError,
    FabricCapabilityUnavailableError,
    FabricNotFoundError,
)
from orqentis.fabric.rest.auth import FABRIC_SCOPE
from orqentis.fabric.rest.client import RestFabricClient
from tests.live.conftest import LAKEHOUSE, MODEL, NOTEBOOK, PIPELINE, REPORT, SANDBOX_WORKSPACES, Sandbox

HISTORY_HINT = "run scripts/live-estate/seed-history.sh to leave one failed run of each kind in the TEST pipeline"
ENV_SUFFIXES = {"[DEV]": Environment.DEV, "[TEST]": Environment.TEST, "[PROD]": Environment.PROD}


# ------------------------------------------------------------------------------------- workspaces
async def test_viewer_sees_exactly_the_sandbox_workspaces(sandbox: Sandbox) -> None:
    assert set(sandbox.workspaces) == set(SANDBOX_WORKSPACES)
    capacity_ids = {w.capacity_id for w in sandbox.workspaces.values()}
    assert len(capacity_ids) == 1 and None not in capacity_ids  # all four sit on the one sandbox capacity


async def test_environment_is_inferred_from_the_workspace_name(sandbox: Sandbox) -> None:
    for name, expected in SANDBOX_WORKSPACES.items():
        assert sandbox.workspaces[name].environment is expected, name
    for suffix, expected in ENV_SUFFIXES.items():
        assert sandbox.workspace(suffix).environment is expected
    assert sandbox.workspace("Consumer").environment is Environment.UNKNOWN  # no [ENV] marker: never guessed
    assert all(w.tenant_id for w in sandbox.workspaces.values())


async def test_get_workspace_maps_the_same_fields_as_the_list(rest_client: RestFabricClient, sandbox: Sandbox) -> None:
    dev = sandbox.workspace("[DEV]")
    assert await rest_client.workspaces.get_workspace(dev.id) == dev


# -------------------------------------------------------------------------------------------- items
@pytest.mark.parametrize("suffix", list(ENV_SUFFIXES))
async def test_environment_workspace_items_are_mapped_by_type(
    rest_client: RestFabricClient, sandbox: Sandbox, suffix: str
) -> None:
    workspace = sandbox.workspace(suffix)
    items = await rest_client.items.list_items(workspace.id)
    by_name = {(i.item_type, i.display_name): i for i in items}

    assert isinstance(by_name[(ItemType.LAKEHOUSE, LAKEHOUSE)], Lakehouse)
    assert isinstance(by_name[(ItemType.NOTEBOOK, NOTEBOOK)], Notebook)
    assert isinstance(by_name[(ItemType.NOTEBOOK, "seed_customers")], Notebook)
    assert isinstance(by_name[(ItemType.DATA_PIPELINE, PIPELINE)], Pipeline)
    # The lakehouse's SQL analytics endpoint is its own item; it is kept, as a generic item, never dropped.
    endpoint = by_name[(ItemType.OTHER, LAKEHOUSE)]
    assert isinstance(endpoint, GenericItem) and endpoint.fabric_type == "SQLEndpoint"
    assert {i.environment for i in items} == {ENV_SUFFIXES[suffix]}
    assert {i.workspace_id for i in items} == {workspace.id}


async def test_item_type_filter_and_get_item(rest_client: RestFabricClient, sandbox: Sandbox) -> None:
    dev = sandbox.workspace("[DEV]")
    notebooks = await rest_client.items.list_items(dev.id, item_type=ItemType.NOTEBOOK)
    assert {n.display_name for n in notebooks} == {NOTEBOOK, "seed_customers"}
    assert all(isinstance(n, Notebook) for n in notebooks)
    fetched = await rest_client.items.get_item(dev.id, notebooks[0].id)
    assert fetched == notebooks[0]
    assert await rest_client.items.list_items(dev.id, item_type=ItemType.DATA_PRODUCT) == []


async def test_consumer_workspace_has_the_semantic_model_and_report(
    rest_client: RestFabricClient, sandbox: Sandbox
) -> None:
    items = await rest_client.items.list_items(sandbox.workspace("Consumer").id)
    by_name = {i.display_name: i for i in items}
    assert isinstance(by_name[MODEL], SemanticModel)
    assert isinstance(by_name[REPORT], Report)


# ---------------------------------------------------------------------------------- job instances
async def _pipeline(rest_client: RestFabricClient, sandbox: Sandbox, suffix: str) -> tuple[str, str]:
    workspace = sandbox.workspace(suffix)
    items = await rest_client.items.list_items(workspace.id, item_type=ItemType.DATA_PIPELINE)
    return workspace.id, next(i.id for i in items if i.display_name == PIPELINE)


async def test_job_instances_are_mapped_and_environment_is_stamped(
    rest_client: RestFabricClient, sandbox: Sandbox
) -> None:
    workspace_id, pipeline_id = await _pipeline(rest_client, sandbox, "[TEST]")
    runs = await rest_client.jobs.list_job_instances(workspace_id, pipeline_id)

    assert len(runs) >= 4, HISTORY_HINT
    assert all(r.item_id == pipeline_id and r.workspace_id == workspace_id for r in runs)
    assert all(r.environment is Environment.TEST and r.item_type is ItemType.DATA_PIPELINE for r in runs)
    assert all(r.start_time is not None and r.start_time.tzinfo is not None for r in runs)  # naive 7-digit UTC parsed
    assert all(r.start_time is not None and datetime.now(UTC) - r.start_time < timedelta(days=31) for r in runs)
    assert RunStatus.SUCCEEDED in {r.status for r in runs}, HISTORY_HINT
    finished = [r for r in runs if r.status.is_terminal]
    assert all(r.end_time is not None and r.end_time >= (r.start_time or r.end_time) for r in finished)
    for failed in (r for r in runs if r.status is RunStatus.FAILED):
        assert failed.failure is not None and failed.failure.message and failed.failure.error_code
    assert all(r.correlation_id for r in runs)  # rootActivityId


async def test_list_item_job_instances_comes_back_newest_first_from_the_service(
    rest_client: RestFabricClient, sandbox: Sandbox
) -> None:
    """Observed (Fabric documents no ordering): the raw service order, before the adapter's own sort touches it.

    The adapter does not depend on this; it sorts and never stops reading early. If this ever fails, the order
    changed, and nothing else is affected.
    """
    workspace_id, pipeline_id = await _pipeline(rest_client, sandbox, "[TEST]")
    http = rest_client.jobs._ctx.http
    url = rest_client.jobs._instances_url(workspace_id, pipeline_id)
    raw = await http.paginate(url, scope=FABRIC_SCOPE)

    starts = [str(r["startTimeUtc"]) for r in raw]
    assert len(starts) >= 4, HISTORY_HINT
    assert starts == sorted(starts, reverse=True), "service order is not newest-first: " + ", ".join(starts)

    adapter = await rest_client.jobs.list_job_instances(workspace_id, pipeline_id, limit=2)
    assert [r.id for r in adapter] == [r["id"] for r in raw[:2]]  # limit keeps the newest


async def test_get_job_instance_matches_the_listed_instance(rest_client: RestFabricClient, sandbox: Sandbox) -> None:
    workspace_id, pipeline_id = await _pipeline(rest_client, sandbox, "[TEST]")
    listed = (await rest_client.jobs.list_job_instances(workspace_id, pipeline_id))[0]
    single = await rest_client.jobs.get_job_instance(workspace_id, pipeline_id, listed.id)
    # failureReason.requestId is minted per response, so it is the one field that legitimately differs.
    ignore = {"failure": {"request_id"}}
    assert single.model_dump(exclude=ignore) == listed.model_dump(exclude=ignore)


# --------------------------------------------------------------------------------- activity runs
async def _failed_activity(
    rest_client: RestFabricClient, sandbox: Sandbox, status: RunStatus, *, activity: str | None = None
) -> tuple[PipelineRun, ActivityRun]:
    workspace_id, pipeline_id = await _pipeline(rest_client, sandbox, "[TEST]")
    for run in await rest_client.jobs.list_job_instances(workspace_id, pipeline_id):
        if run.status is not RunStatus.FAILED:
            continue
        for act in await rest_client.pipelines.query_activity_runs(workspace_id, pipeline_id, run.id):
            if act.status is status and (activity is None or act.activity_name == activity):
                return run, act
    pytest.fail(f"no failed run with a {status.value} activity in the TEST pipeline history; {HISTORY_HINT}")


async def test_succeeded_notebook_activity_targets_the_notebook(
    rest_client: RestFabricClient, sandbox: Sandbox
) -> None:
    workspace_id, pipeline_id = await _pipeline(rest_client, sandbox, "[DEV]")
    run = next(
        r for r in await rest_client.jobs.list_job_instances(workspace_id, pipeline_id) if r.status.name == "SUCCEEDED"
    )
    activities = await rest_client.pipelines.query_activity_runs(workspace_id, pipeline_id, run.id)
    notebook = next(
        i
        for i in await rest_client.items.list_items(workspace_id, item_type=ItemType.NOTEBOOK)
        if i.display_name == NOTEBOOK
    )

    assert [a.activity_name for a in activities] == [NOTEBOOK]
    activity = activities[0]
    assert activity.activity_type == "TridentNotebook"
    assert activity.status is RunStatus.SUCCEEDED
    assert activity.target_item_id == notebook.id
    assert activity.error is None  # Fabric sends an all-empty error object on success
    assert activity.start_time and activity.end_time and activity.end_time > activity.start_time
    assert activity.pipeline_run_id == run.id and activity.correlation_id == run.correlation_id


async def test_failed_notebook_activity_carries_the_notebook_exception(
    rest_client: RestFabricClient, sandbox: Sandbox
) -> None:
    run, activity = await _failed_activity(rest_client, sandbox, RunStatus.FAILED, activity=NOTEBOOK)
    assert activity.activity_type == "TridentNotebook"
    assert activity.error is not None and "Notebook execution failed" in activity.error.message
    assert activity.target_item_id is not None
    assert run.failure is not None and run.failure.error_code == "2451"  # "Operation on target ... failed"


async def test_timed_out_activity_is_reported_as_timed_out_with_a_message(
    rest_client: RestFabricClient, sandbox: Sandbox
) -> None:
    """Regression: Fabric sends status TimedOut with an all-empty error object; the adapter must say 'timed out'."""
    _, activity = await _failed_activity(rest_client, sandbox, RunStatus.TIMED_OUT)
    assert activity.status.is_failure
    assert activity.error is not None
    assert "timed out" in activity.error.message and activity.error.error_code == "ActivityTimedOut"


async def test_auth_style_failure_surfaces_the_source_error(rest_client: RestFabricClient, sandbox: Sandbox) -> None:
    _, activity = await _failed_activity(rest_client, sandbox, RunStatus.FAILED, activity="call_crm_source")
    assert activity.activity_type == "WebActivity"
    assert activity.error is not None and activity.error.message
    assert activity.target_item_id is None  # a Web call acts on no Fabric item


# --------------------------------------------------------------------------------- semantic model
async def test_refresh_history_needs_more_than_viewer(rest_client: RestFabricClient, sandbox: Sandbox) -> None:
    """Finding: Power BI's refresh history is write-gated. Viewer reads the model but gets 403 on refreshes."""
    consumer = sandbox.workspace("Consumer")
    model = next(i for i in await rest_client.items.list_items(consumer.id) if i.display_name == MODEL)
    with pytest.raises(FabricAuthError) as caught:
        await rest_client.semantic_models.get_refresh_history(consumer.id, model.id, top=5)
    error = caught.value
    assert error.status_code == 403 and error.error_code == "Unauthorized" and not error.retryable
    assert error.request_id and error.correlation_id
    assert "bearer" not in error.message.lower()
    assert "<eupi>" not in error.message and "[redacted]" in error.message  # the principal's own id is not echoed


async def test_semantic_model_has_no_fabric_job_instances(rest_client: RestFabricClient, sandbox: Sandbox) -> None:
    consumer = sandbox.workspace("Consumer")
    model = next(i for i in await rest_client.items.list_items(consumer.id) if i.display_name == MODEL)
    with pytest.raises(FabricNotFoundError) as caught:
        await rest_client.jobs.list_job_instances(consumer.id, model.id)
    assert caught.value.error_code == "ItemNotFound"


# ------------------------------------------------------------------------------------ error mapping
async def test_unknown_item_maps_to_not_found_with_correlation(rest_client: RestFabricClient, sandbox: Sandbox) -> None:
    dev = sandbox.workspace("[DEV]")
    with pytest.raises(FabricNotFoundError) as caught:
        await rest_client.items.get_item(dev.id, "11111111-2222-4333-8444-555555555555")
    error = caught.value
    assert error.status_code == 404 and not error.retryable
    assert error.error_code and error.request_id and error.correlation_id


async def test_unknown_workspace_is_not_readable(rest_client: RestFabricClient) -> None:
    with pytest.raises((FabricNotFoundError, FabricAuthError)) as caught:
        await rest_client.workspaces.get_workspace("11111111-2222-4333-8444-555555555555")
    assert caught.value.status_code in (403, 404)


# ----------------------------------------------------------------- capacities, deployment, lineage
async def test_capacity_listing_shows_only_what_viewer_can_see(rest_client: RestFabricClient, sandbox: Sandbox) -> None:
    """Finding: the F4 sandbox capacity is invisible to the Viewer-only principal (capacity listing needs a
    capacity role); the listing returns only capacities open to everyone."""
    sandbox_capacity = sandbox.workspace("[DEV]").capacity_id
    capacities = await rest_client.capacities.list_capacities()
    assert all(c.sku and c.state for c in capacities)
    assert sandbox_capacity not in {c.id for c in capacities}


async def test_capacity_metrics_are_reported_unavailable_not_faked(
    rest_client: RestFabricClient, sandbox: Sandbox
) -> None:
    capacity_id = sandbox.workspace("[DEV]").capacity_id or ""
    now = datetime.now(UTC)
    with pytest.raises(FabricCapabilityUnavailableError):
        await rest_client.capacities.get_capacity_metrics(capacity_id, start=now - timedelta(hours=1), end=now)


async def test_deployment_state_is_empty_without_deployment_pipelines(
    rest_client: RestFabricClient, sandbox: Sandbox
) -> None:
    pipeline_id = (await _pipeline(rest_client, sandbox, "[DEV]"))[1]
    assert await rest_client.deployments.get_deployment_states(pipeline_id) == []
