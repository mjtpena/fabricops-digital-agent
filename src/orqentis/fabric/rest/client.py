"""Read-only REST adapter for Microsoft Fabric and Power BI.

Implements :class:`~orqentis.fabric.ports.FabricClient` over the public Fabric REST API
(workspaces, items, job instances, pipeline activity runs, capacities, deployment pipelines) and the
Power BI REST API (semantic model refresh history). It exposes no mutation of any kind; the single
POST it sends (``queryactivityruns``) is a read-only query.

What has no public REST API is not faked: capacity CU utilisation raises
:class:`~orqentis.fabric.errors.FabricCapabilityUnavailableError`, and lineage comes from the declared,
version-controlled contracts.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import httpx

from orqentis.config.clock import Clock, SystemClock
from orqentis.config.settings import PUBLIC_KUSTO_SCOPE, PUBLIC_ONELAKE_BASE_URL, PUBLIC_STORAGE_SCOPE, Settings
from orqentis.contracts.model import EstateContracts
from orqentis.domain.common import Environment, ItemType
from orqentis.domain.deployment import DeploymentState
from orqentis.domain.estate import (
    Capacity,
    Dataflow,
    FabricItem,
    GenericItem,
    Lakehouse,
    Notebook,
    Pipeline,
    Report,
    SemanticModel,
    SparkJobDefinition,
    Warehouse,
    Workspace,
)
from orqentis.domain.runs import ActivityRun, CapacitySnapshot, FailureDetail, PipelineRun, Refresh, RunStatus
from orqentis.domain.topology import Dependency
from orqentis.fabric.errors import (
    FabricAuthError,
    FabricCapabilityUnavailableError,
    FabricError,
    FabricNotFoundError,
)
from orqentis.fabric.ports import FabricClient
from orqentis.fabric.rest.auth import FABRIC_SCOPE, POWERBI_SCOPE, TokenProvider
from orqentis.fabric.rest.budget import FabricCallBudget
from orqentis.fabric.rest.capacity_metrics import CapacityMetricsReader
from orqentis.fabric.rest.http import FabricHttp, JsonObject, strip_privacy_tags
from orqentis.fabric.rest.monitoring import RestMonitoringService
from orqentis.fabric.rest.onelake import DEFAULT_MAX_COMMIT_BYTES, RestDeltaLogService
from orqentis.fabric.rest.util import clean as _clean
from orqentis.fabric.rest.util import iso as _iso
from orqentis.fabric.rest.util import parse_datetime
from orqentis.fabric.rest.util import segment as _segment
from orqentis.observability.logging import get_logger
from orqentis.topology.environment import (
    EnvironmentChange,
    EnvironmentResolver,
    EnvironmentSource,
    capacity_change,
    infer_environment,  # noqa: F401  (re-exported: callers import it from this module)
    item_change,
    stage_environment,
    workspace_change,
)

_log = get_logger(__name__)

# ------------------------------------------------------------------------------------ vocabularies
_JOB_STATUS = {
    "NotStarted": RunStatus.NOT_STARTED,
    "InProgress": RunStatus.IN_PROGRESS,
    "Completed": RunStatus.SUCCEEDED,
    "Failed": RunStatus.FAILED,
    "Cancelled": RunStatus.CANCELLED,
    "Deduped": RunStatus.DEDUPED,
}
_ACTIVITY_STATUS = {
    "Queued": RunStatus.QUEUED,
    "InProgress": RunStatus.IN_PROGRESS,
    "Succeeded": RunStatus.SUCCEEDED,
    "Completed": RunStatus.SUCCEEDED,
    "Failed": RunStatus.FAILED,
    "Cancelled": RunStatus.CANCELLED,
    "Canceled": RunStatus.CANCELLED,
    "Skipped": RunStatus.SKIPPED,
    "TimedOut": RunStatus.TIMED_OUT,
}
_REFRESH_STATUS = {
    "Unknown": RunStatus.IN_PROGRESS,  # Power BI reports an in-flight refresh as "Unknown"
    "NotStarted": RunStatus.NOT_STARTED,
    "InProgress": RunStatus.IN_PROGRESS,
    "Completed": RunStatus.SUCCEEDED,
    "Failed": RunStatus.FAILED,
    "Cancelled": RunStatus.CANCELLED,
    "Disabled": RunStatus.CANCELLED,
}
# Fabric job types by item (lower case: the API's casing is not documented to be stable).
# "Refresh" is a Dataflow refresh.
_JOB_ITEM_TYPES = {
    "pipeline": ItemType.DATA_PIPELINE,
    "runnotebook": ItemType.NOTEBOOK,
    "sparkjob": ItemType.SPARK_JOB_DEFINITION,
    "refresh": ItemType.DATAFLOW,
}

_ItemClass = type[Pipeline | Notebook | SparkJobDefinition | Dataflow | Lakehouse | Warehouse | SemanticModel | Report]
_ITEM_CLASSES: dict[str, _ItemClass] = {
    ItemType.DATA_PIPELINE.value: Pipeline,
    ItemType.NOTEBOOK.value: Notebook,
    ItemType.SPARK_JOB_DEFINITION.value: SparkJobDefinition,
    ItemType.DATAFLOW.value: Dataflow,
    ItemType.LAKEHOUSE.value: Lakehouse,
    ItemType.WAREHOUSE.value: Warehouse,
    ItemType.SEMANTIC_MODEL.value: SemanticModel,
    ItemType.REPORT.value: Report,
}
# Keys under which pipeline activity inputs reference the Fabric item they act on.
_TARGET_ITEM_KEYS = ("notebookId", "semanticModelId", "datasetId", "pipelineId", "artifactId")

# Activity runs are filtered by last-update time; widen the job's own window to absorb clock skew.
_ACTIVITY_WINDOW_MARGIN = timedelta(hours=1)
_MAX_ACTIVITY_PAGES = 50


# ----------------------------------------------------------------------------------------- helpers
def _required(raw: JsonObject, key: str, what: str) -> str:
    value = _clean(raw.get(key))
    if value is None:
        raise FabricError(f"Fabric response for {what} is missing '{key}'", error_code="InvalidResponse")
    return value


def _compact(**values: Any) -> dict[str, Any]:
    return {k: v for k, v in values.items() if v is not None}


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _status(raw: Any, mapping: dict[str, RunStatus]) -> RunStatus:
    text = _clean(raw)
    if text is None:
        return RunStatus.IN_PROGRESS
    if text in mapping:
        return mapping[text]
    try:
        return RunStatus(text)
    except ValueError:
        # Unknown vendor status: keep it non-terminal so it is never reported as a success or failure.
        return RunStatus.IN_PROGRESS


def _failure(raw: Any) -> FailureDetail | None:
    """``{errorCode, message, failureType, requestId}`` -> FailureDetail; empty payloads mean no error."""
    if not isinstance(raw, dict):
        return None
    message, code = _clean(raw.get("message")), _clean(raw.get("errorCode"))
    if message is not None:
        message = _clean(strip_privacy_tags(message))  # activity and job errors can carry Power BI privacy markers
    if message is None and code is None:
        return None
    return FailureDetail(
        message=message or code or "",
        error_code=code,
        failure_type=_clean(raw.get("failureType")),
        request_id=_clean(raw.get("requestId")),
    )


def _refresh_error(raw: Any) -> FailureDetail | None:
    """Power BI ``serviceExceptionJson`` (a JSON *string*) -> FailureDetail."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    payload: Any = raw
    if isinstance(raw, str):
        try:
            payload = json.loads(raw)
        except ValueError:
            return FailureDetail(message=strip_privacy_tags(raw.strip())[:2000])
    if not isinstance(payload, dict):
        return FailureDetail(message=strip_privacy_tags(str(payload))[:2000])
    error = payload["error"] if isinstance(payload.get("error"), dict) else payload
    code = _clean(error.get("errorCode") or error.get("code"))
    message = _clean(error.get("errorDescription") or error.get("message") or error.get("description"))
    if message is not None:
        message = strip_privacy_tags(message)
    if message is None and code is None:
        return None
    return FailureDetail(message=message or code or "", error_code=code)


def _maybe_json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def target_item_id(activity_input: Any) -> str | None:
    """Best-effort id of the Fabric item a pipeline activity acts on, from its ``input`` payload."""
    payload = _maybe_json(activity_input)
    if not isinstance(payload, dict):
        return None
    nested = [v for v in payload.values() if isinstance(v, dict)]
    for scope in (payload, *nested):
        for key in _TARGET_ITEM_KEYS:
            if (value := _clean(scope.get(key))) is not None:
                return value
    return None


def _item_type(fabric_type: str | None) -> ItemType:
    if fabric_type in _ITEM_CLASSES:
        return ItemType(fabric_type)
    return ItemType.OTHER


EnvironmentObserver = Callable[[EnvironmentChange], Awaitable[None]]


class _EnvironmentFeed:
    """Tells an observer, the first time a node is mapped, that a declaration changed its classification.

    The services map responses synchronously, so changes are noted there and handed to the observer (which is
    async: it writes the audit trail) by ``flush`` once the call completes. Without an observer nothing is kept.
    """

    def __init__(self) -> None:
        self.observer: EnvironmentObserver | None = None
        self._pending: list[EnvironmentChange] = []

    def note(self, change: EnvironmentChange | None) -> None:
        if change is not None and self.observer is not None:
            self._pending.append(change)

    async def flush(self) -> None:
        pending, self._pending = self._pending, []
        if self.observer is not None:
            for change in pending:
                await self.observer(change)


@dataclass(frozen=True)
class _Context:
    """What every service needs: the HTTP client, the API base URLs, the tenant id and the environments."""

    http: FabricHttp
    fabric_base_url: str
    powerbi_base_url: str
    tenant_id: str
    environments: EnvironmentResolver
    feed: _EnvironmentFeed = field(default_factory=_EnvironmentFeed)

    def fabric(self, *segments: str) -> str:
        return "/".join((self.fabric_base_url.rstrip("/"), *(_segment(s) for s in segments)))

    def powerbi(self, *segments: str) -> str:
        return "/".join((self.powerbi_base_url.rstrip("/"), *(_segment(s) for s in segments)))


# ---------------------------------------------------------------------------------------- services
class RestWorkspaceService:
    def __init__(self, ctx: _Context) -> None:
        self._ctx = ctx
        self._environments: dict[str, Environment] = {}

    def _map(self, raw: JsonObject) -> Workspace:
        display_name = _required(raw, "displayName", "workspace")
        workspace_id = _required(raw, "id", "workspace")
        resolved = self._ctx.environments.resolve_workspace(workspace_id, display_name)  # declared > inferred
        workspace = Workspace(
            id=workspace_id,
            tenant_id=self._ctx.tenant_id,
            display_name=display_name,
            capacity_id=_clean(raw.get("capacityId")),
            environment=resolved.environment,
            description=_clean(raw.get("description")),
            metadata=_compact(
                workspace_type=_clean(raw.get("type")),
                domain_id=_clean(raw.get("domainId")),
                # Provenance is recorded only when a contracts declaration decided the environment.
                environment_source=None if resolved.source is EnvironmentSource.INFERRED else resolved.source.value,
            ),
        )
        self._environments[workspace.id] = workspace.environment
        self._ctx.feed.note(workspace_change(workspace))
        return workspace

    async def list_workspaces(self) -> list[Workspace]:
        raw = await self._ctx.http.paginate(self._ctx.fabric("workspaces"), scope=FABRIC_SCOPE)
        workspaces = [self._map(entry) for entry in raw]
        await self._ctx.feed.flush()
        return workspaces

    async def get_workspace(self, workspace_id: str) -> Workspace:
        raw = await self._ctx.http.get_json(self._ctx.fabric("workspaces", workspace_id), scope=FABRIC_SCOPE)
        workspace = self._map(raw)
        await self._ctx.feed.flush()
        return workspace

    def known_environment(self, workspace_id: str) -> Environment:
        """Environment of an already-seen workspace (no network call); UNKNOWN otherwise."""
        return self._environments.get(workspace_id, Environment.UNKNOWN)

    async def environment_of(self, workspace_id: str) -> Environment:
        """Environment of a workspace, fetching it once if it has not been seen yet."""
        if workspace_id not in self._environments:
            await self.get_workspace(workspace_id)
        return self._environments.get(workspace_id, Environment.UNKNOWN)


class RestItemService:
    def __init__(self, ctx: _Context, workspaces: RestWorkspaceService) -> None:
        self._ctx = ctx
        self._workspaces = workspaces

    def _map(self, raw: JsonObject, workspace_id: str, environment: Environment) -> FabricItem:
        fabric_type = _clean(raw.get("type"))
        tags = [
            name for tag in raw.get("tags") or [] if isinstance(tag, dict) and (name := _clean(tag.get("displayName")))
        ]
        item_id = _required(raw, "id", "item")
        resolved = self._ctx.environments.resolve_item(item_id, environment)  # workspace's unless declared
        fields: dict[str, Any] = {
            "id": item_id,
            "tenant_id": self._ctx.tenant_id,
            "workspace_id": _clean(raw.get("workspaceId")) or workspace_id,
            "display_name": _required(raw, "displayName", "item"),
            "description": _clean(raw.get("description")),
            "environment": resolved.environment,
            "tags": tags,
            "metadata": _compact(
                folder_id=_clean(raw.get("folderId")),
                environment_source=None if resolved.source is EnvironmentSource.INHERITED else resolved.source.value,
            ),
        }
        item_class = _ITEM_CLASSES.get(fabric_type or "")
        item: FabricItem = (
            item_class(**fields) if item_class is not None else GenericItem(**fields, fabric_type=fabric_type)
        )
        self._ctx.feed.note(item_change(item, environment))
        return item

    async def list_items(self, workspace_id: str, *, item_type: ItemType | None = None) -> list[FabricItem]:
        if item_type in (ItemType.DATA_PRODUCT, ItemType.EXTERNAL_SOURCE):
            return []  # logical nodes are declared in contracts; Fabric never returns them
        params: dict[str, str] | None = None
        if item_type is not None and item_type.value in _ITEM_CLASSES:
            params = {"type": item_type.value}  # server-side filter; OTHER is filtered client-side below
        raw_items = await self._ctx.http.paginate(
            self._ctx.fabric("workspaces", workspace_id, "items"), scope=FABRIC_SCOPE, params=params
        )
        environment = await self._workspaces.environment_of(workspace_id)
        items = [self._map(raw, workspace_id, environment) for raw in raw_items]
        await self._ctx.feed.flush()
        return [i for i in items if item_type is None or i.item_type == item_type]

    async def get_item(self, workspace_id: str, item_id: str) -> FabricItem:
        raw = await self._ctx.http.get_json(
            self._ctx.fabric("workspaces", workspace_id, "items", item_id), scope=FABRIC_SCOPE
        )
        item = self._map(raw, workspace_id, await self._workspaces.environment_of(workspace_id))
        await self._ctx.feed.flush()
        return item


class RestJobService:
    def __init__(self, ctx: _Context, workspaces: RestWorkspaceService, *, max_pages: int = 10) -> None:
        self._ctx = ctx
        self._workspaces = workspaces
        self._max_pages = max_pages

    def _map(self, raw: JsonObject, workspace_id: str, item_id: str) -> PipelineRun:
        job_type, fabric_status = _clean(raw.get("jobType")), _clean(raw.get("status"))
        run_item_id = _clean(raw.get("itemId")) or item_id
        return PipelineRun(
            id=_required(raw, "id", "job instance"),
            tenant_id=self._ctx.tenant_id,
            workspace_id=workspace_id,
            item_id=run_item_id,
            item_type=_JOB_ITEM_TYPES.get((job_type or "Pipeline").lower(), ItemType.OTHER),
            # The item's own environment: its workspace's, unless an item_id declaration says otherwise.
            environment=self._ctx.environments.item(run_item_id, self._workspaces.known_environment(workspace_id)),
            status=_status(fabric_status, _JOB_STATUS),
            invoke_type=_clean(raw.get("invokeType")),
            start_time=parse_datetime(raw.get("startTimeUtc")),
            end_time=parse_datetime(raw.get("endTimeUtc")),
            failure=_failure(raw.get("failureReason")),
            correlation_id=_clean(raw.get("rootActivityId")),
            metadata=_compact(job_type=job_type, fabric_status=fabric_status),
        )

    def _instances_url(self, workspace_id: str, item_id: str, *extra: str) -> str:
        return self._ctx.fabric("workspaces", workspace_id, "items", item_id, "jobs", "instances", *extra)

    async def list_job_instances(
        self, workspace_id: str, item_id: str, *, limit: int = 50, since: datetime | None = None
    ) -> list[PipelineRun]:
        """Recent runs of an item, newest start first.

        Fabric documents no ordering for "List Item Job Instances" (only a ``continuationToken``; it keeps about
        the 100 most recent completed runs plus every active one), so this never stops reading pages early: an
        early stop would be wrong even for a start-time ordering, because a long-running job that started before
        the watermark can still finish after it. ``since`` therefore filters after reading, and ``max_pages``
        bounds the cost of one listing.
        """
        if limit <= 0:
            return []
        url = self._instances_url(workspace_id, item_id)
        raw_runs = await self._ctx.http.paginate(url, scope=FABRIC_SCOPE, max_pages=self._max_pages)
        runs = [self._map(raw, workspace_id, item_id) for raw in raw_runs]
        if since is not None:
            runs = [r for r in runs if r.observed_at is None or r.observed_at >= since]
        runs.sort(key=lambda r: r.start_time.timestamp() if r.start_time else 0.0, reverse=True)
        return runs[:limit]

    async def get_job_instance(self, workspace_id: str, item_id: str, job_instance_id: str) -> PipelineRun:
        raw = await self._ctx.http.get_json(
            self._instances_url(workspace_id, item_id, job_instance_id), scope=FABRIC_SCOPE
        )
        return self._map(raw, workspace_id, item_id)


class RestPipelineService:
    def __init__(self, ctx: _Context, jobs: RestJobService, clock: Clock) -> None:
        self._ctx = ctx
        self._jobs = jobs
        self._clock = clock

    def _map(self, raw: JsonObject, *, workspace_id: str, pipeline_id: str, run: PipelineRun) -> ActivityRun:
        activity_name = _required(raw, "activityName", "activity run")
        output = _maybe_json(raw.get("output"))
        fabric_status = _clean(raw.get("status"))
        status = _status(fabric_status, _ACTIVITY_STATUS)
        error = _failure(raw.get("error"))
        if error is None and status is RunStatus.TIMED_OUT:
            # Observed live: an activity that exceeds its timeout comes back with status "TimedOut" and an
            # error object whose message and errorCode are both empty. Say so, or the timeout is invisible to
            # error classification (the pipeline-level message is a generic "InternalServerError").
            raw_error = raw.get("error")
            error = FailureDetail(
                message=f"Activity '{activity_name}' timed out: it exceeded its configured timeout",
                error_code="ActivityTimedOut",
                failure_type=_clean(raw_error.get("failureType")) if isinstance(raw_error, dict) else None,
            )
        return ActivityRun(
            id=_clean(raw.get("activityRunId")) or f"{run.id}:{activity_name}",
            pipeline_run_id=run.id,
            tenant_id=self._ctx.tenant_id,
            workspace_id=workspace_id,
            pipeline_item_id=pipeline_id,
            activity_name=activity_name,
            activity_type=_clean(raw.get("activityType")) or "Unknown",
            status=status,
            start_time=parse_datetime(raw.get("activityRunStart")),
            end_time=parse_datetime(raw.get("activityRunEnd")),
            target_item_id=target_item_id(raw.get("input")),
            error=error,
            output=output if isinstance(output, dict) else ({} if output is None else {"value": output}),
            correlation_id=run.correlation_id,
            metadata=_compact(
                fabric_status=fabric_status, duration_ms=raw.get("durationInMs"), retry_attempt=raw.get("retryAttempt")
            ),
        )

    async def query_activity_runs(self, workspace_id: str, pipeline_id: str, job_instance_id: str) -> list[ActivityRun]:
        # The job instance bounds the lastUpdated window and supplies the root activity id.
        run = await self._jobs.get_job_instance(workspace_id, pipeline_id, job_instance_id)
        if run.start_time is None:
            return []  # not started yet: no activity has run
        url = self._ctx.fabric(
            "workspaces", workspace_id, "datapipelines", "pipelineruns", job_instance_id, "queryactivityruns"
        )
        body: dict[str, Any] = {
            "filters": [],
            "orderBy": [{"orderBy": "ActivityRunStart", "order": "ASC"}],
            "lastUpdatedAfter": _iso(run.start_time - _ACTIVITY_WINDOW_MARGIN),
            "lastUpdatedBefore": _iso((run.end_time or self._clock.now()) + _ACTIVITY_WINDOW_MARGIN),
        }
        raw_runs: list[JsonObject] = []
        seen: set[str] = set()
        for _ in range(_MAX_ACTIVITY_PAGES):
            # POST, but a read-only query: it returns activity runs and changes nothing in Fabric.
            page = await self._ctx.http.post_json(url, scope=FABRIC_SCOPE, json=body)
            raw_runs.extend(v for v in page.get("value") or [] if isinstance(v, dict))
            token = _clean(page.get("continuationToken"))
            if token is None or token in seen:
                break
            seen.add(token)
            body = {**body, "continuationToken": token}
        return [self._map(r, workspace_id=workspace_id, pipeline_id=pipeline_id, run=run) for r in raw_runs]


class RestSemanticModelService:
    def __init__(self, ctx: _Context) -> None:
        self._ctx = ctx

    def _map(self, raw: JsonObject, workspace_id: str, semantic_model_id: str) -> Refresh:
        request_id = _clean(raw.get("requestId"))
        numeric_id = raw.get("id")
        powerbi_status = _clean(raw.get("status"))
        attempts = raw.get("refreshAttempts")
        local_id = numeric_id if numeric_id is not None else raw.get("startTime")
        return Refresh(
            # requestId is globally unique (and is the refresh id of the enhanced-refresh API).
            id=request_id or f"{semantic_model_id}:{local_id}",
            tenant_id=self._ctx.tenant_id,
            workspace_id=workspace_id,
            item_id=semantic_model_id,
            refresh_type=_clean(raw.get("refreshType")) or "Scheduled",
            status=_status(powerbi_status, _REFRESH_STATUS),
            start_time=parse_datetime(raw.get("startTime")),
            end_time=parse_datetime(raw.get("endTime")),
            error=_refresh_error(raw.get("serviceExceptionJson")),
            correlation_id=request_id,
            metadata=_compact(
                refresh_id=numeric_id,
                powerbi_status=powerbi_status,
                extended_status=_clean(raw.get("extendedStatus")),
                refresh_attempts=len(attempts) if isinstance(attempts, list) else None,
            ),
        )

    async def get_refresh_history(self, workspace_id: str, semantic_model_id: str, *, top: int = 10) -> list[Refresh]:
        if top <= 0:
            return []
        url = self._ctx.powerbi("groups", workspace_id, "datasets", semantic_model_id, "refreshes")
        body = await self._ctx.http.get_json(url, scope=POWERBI_SCOPE, params={"$top": top})
        refreshes = [
            self._map(raw, workspace_id, semantic_model_id) for raw in body.get("value") or [] if isinstance(raw, dict)
        ]
        refreshes.sort(key=lambda r: r.start_time.timestamp() if r.start_time else 0.0, reverse=True)
        return refreshes[:top]


class RestCapacityService:
    def __init__(self, ctx: _Context, metrics: CapacityMetricsReader | None = None) -> None:
        self._ctx = ctx
        self._metrics = metrics

    async def list_capacities(self) -> list[Capacity]:
        capacities: list[Capacity] = []
        for raw in await self._ctx.http.paginate(self._ctx.fabric("capacities"), scope=FABRIC_SCOPE):
            display_name = _required(raw, "displayName", "capacity")
            capacity_id = _required(raw, "id", "capacity")
            resolved = self._ctx.environments.resolve_capacity(capacity_id, display_name)  # declared > inferred
            capacities.append(
                Capacity(
                    id=capacity_id,
                    tenant_id=self._ctx.tenant_id,
                    display_name=display_name,
                    sku=_clean(raw.get("sku")) or "Unknown",
                    region=_clean(raw.get("region")),
                    state=_clean(raw.get("state")) or "Unknown",
                    environment=resolved.environment,
                    metadata=_compact(
                        environment_source=None
                        if resolved.source is EnvironmentSource.INFERRED
                        else resolved.source.value
                    ),
                )
            )
        for capacity in capacities:
            self._ctx.feed.note(capacity_change(capacity))
        await self._ctx.feed.flush()
        return capacities

    async def get_capacity_metrics(self, capacity_id: str, *, start: datetime, end: datetime) -> list[CapacitySnapshot]:
        if self._metrics is not None:
            return await self._metrics.get_capacity_metrics(capacity_id, start=start, end=end)
        raise FabricCapabilityUnavailableError(
            "Capacity CU utilisation is not exposed by any public Fabric REST API. Configure the Capacity Metrics "
            "app's workspace and semantic model (ORQENTIS_CAPACITY_METRICS_WORKSPACE_ID and "
            "ORQENTIS_CAPACITY_METRICS_DATASET_ID) to read it through executeQueries, or ingest the capacity "
            "events into an Eventhouse (the ADR-0009 fallback).",
            error_code="CapacityMetricsUnavailable",
            details={"capacity_id": capacity_id, "start": start.isoformat(), "end": end.isoformat()},
        )


@dataclass(frozen=True)
class _Stage:
    id: str
    pipeline_id: str
    pipeline_name: str | None
    display_name: str | None
    order: int
    workspace_id: str | None
    environment: Environment


class RestDeploymentService:
    """Deployment state per stage from Fabric deployment pipelines.

    The same logical item is matched across stages by ``itemType`` + ``itemDisplayName``. Pipeline and
    stage listings are cached for the life of the client; stage *items* are always re-read so
    ``lastDeploymentTime`` is current.
    """

    def __init__(self, ctx: _Context) -> None:
        self._ctx = ctx
        self._pipelines: list[tuple[str, str | None]] | None = None
        self._stages: dict[str, list[_Stage]] = {}
        self._located: dict[str, str] = {}  # item id -> deployment pipeline id
        self._lock = asyncio.Lock()

    async def _list_pipelines(self) -> list[tuple[str, str | None]]:
        async with self._lock:
            if self._pipelines is None:
                url = self._ctx.fabric("deploymentPipelines")
                self._pipelines = [
                    (_required(p, "id", "deployment pipeline"), _clean(p.get("displayName")))
                    for p in await self._ctx.http.paginate(url, scope=FABRIC_SCOPE)
                ]
            return self._pipelines

    def _stage_environment(self, raw: JsonObject, position: int, count: int) -> Environment:
        """A declared environment for the stage's workspace, else the stage's name, else its position."""
        workspace_id = _clean(raw.get("workspaceId"))
        if workspace_id is not None:
            declared = self._ctx.environments.resolve_declared_workspace(workspace_id, _clean(raw.get("workspaceName")))
            if declared is not None:
                return declared.environment
        return stage_environment(_clean(raw.get("displayName")), position, count)

    async def _list_stages(self, pipeline_id: str, pipeline_name: str | None) -> list[_Stage]:
        if pipeline_id not in self._stages:
            url = self._ctx.fabric("deploymentPipelines", pipeline_id, "stages")
            raw = await self._ctx.http.paginate(url, scope=FABRIC_SCOPE)
            ordered = sorted(enumerate(raw), key=lambda p: (_as_int(p[1].get("order"), p[0]), p[0]))
            self._stages[pipeline_id] = [
                _Stage(
                    id=_required(s, "id", "deployment pipeline stage"),
                    pipeline_id=pipeline_id,
                    pipeline_name=pipeline_name,
                    display_name=_clean(s.get("displayName")),
                    order=_as_int(s.get("order"), index),
                    workspace_id=_clean(s.get("workspaceId")),
                    environment=self._stage_environment(s, position, len(ordered)),
                )
                for position, (index, s) in enumerate(ordered)
            ]
        return self._stages[pipeline_id]

    async def _stage_items(self, stage: _Stage) -> list[JsonObject]:
        if stage.workspace_id is None:
            return []  # unassigned stage: nothing deployed
        url = self._ctx.fabric("deploymentPipelines", stage.pipeline_id, "stages", stage.id, "items")
        return await self._ctx.http.paginate(url, scope=FABRIC_SCOPE)

    def _state(self, stage: _Stage, raw: JsonObject) -> DeploymentState:
        item_id = _required(raw, "itemId", "deployment stage item")
        display_name = _required(raw, "itemDisplayName", "deployment stage item")
        fabric_type = _clean(raw.get("itemType"))
        return DeploymentState(
            id=f"{stage.pipeline_id}:{stage.id}:{item_id}",
            tenant_id=self._ctx.tenant_id,
            logical_item_id=f"{fabric_type}:{display_name}",
            item_id=item_id,
            workspace_id=stage.workspace_id or "",
            item_type=_item_type(fabric_type),
            display_name=display_name,
            environment=stage.environment,
            deployed_at=parse_datetime(raw.get("lastDeploymentTime")),
            metadata=_compact(
                deployment_pipeline_id=stage.pipeline_id,
                deployment_pipeline_name=stage.pipeline_name,
                stage_id=stage.id,
                stage_name=stage.display_name,
                stage_order=stage.order,
                source_item_id=_clean(raw.get("sourceItemId")),
                target_item_id=_clean(raw.get("targetItemId")),
            ),
        )

    async def _states_in(
        self,
        pipeline_id: str,
        pipeline_name: str | None,
        item_id: str,
        prefetched: dict[str, list[JsonObject]],
    ) -> list[DeploymentState]:
        items_by_stage: list[tuple[_Stage, list[JsonObject]]] = []
        for stage in await self._list_stages(pipeline_id, pipeline_name):
            stage_items = prefetched[stage.id] if stage.id in prefetched else await self._stage_items(stage)
            items_by_stage.append((stage, stage_items))
        anchor = next(
            (raw for _, stage_items in items_by_stage for raw in stage_items if _clean(raw.get("itemId")) == item_id),
            None,
        )
        if anchor is None:
            return []
        key = (_clean(anchor.get("itemType")), _clean(anchor.get("itemDisplayName")))
        return [
            self._state(stage, raw)
            for stage, items in items_by_stage
            for raw in items
            if (_clean(raw.get("itemType")), _clean(raw.get("itemDisplayName"))) == key
        ]

    async def get_deployment_states(self, item_id: str) -> list[DeploymentState]:
        pipelines = await self._list_pipelines()
        names = dict(pipelines)
        if (known := self._located.get(item_id)) is not None:
            states = await self._states_in(known, names.get(known), item_id, {})
            if states:
                return states
            self._located.pop(item_id, None)  # moved or removed since it was located; rescan
        for pipeline_id, pipeline_name in pipelines:
            prefetched: dict[str, list[JsonObject]] = {}
            for stage in await self._list_stages(pipeline_id, pipeline_name):
                try:
                    stage_items = await self._stage_items(stage)
                except (FabricAuthError, FabricNotFoundError) as exc:
                    # While *searching*, skip stages we cannot read; a stage of the matching pipeline is
                    # re-read strictly below so an incomplete comparison is never returned silently.
                    _log.warning(
                        "deployment stage unreadable",
                        extra={"deployment_pipeline_id": pipeline_id, "stage_id": stage.id, "error": exc.to_dict()},
                    )
                    continue
                prefetched[stage.id] = stage_items
                for raw in stage_items:
                    if (other := _clean(raw.get("itemId"))) is not None:
                        self._located.setdefault(other, pipeline_id)
                if any(_clean(raw.get("itemId")) == item_id for raw in stage_items):
                    return await self._states_in(pipeline_id, pipeline_name, item_id, prefetched)
        return []


class DeclaredDependencyService:
    """Dependencies from the declared, version-controlled estate contracts.

    Fabric has no general-purpose lineage REST API, so topology is declared by platform teams (and
    reviewed like code) rather than discovered.
    """

    def __init__(self, contracts: EstateContracts) -> None:
        self._dependencies = list(contracts.dependencies)

    def replace(self, contracts: EstateContracts) -> None:
        self._dependencies = list(contracts.dependencies)

    async def list_dependencies(self) -> list[Dependency]:
        return list(self._dependencies)


# ------------------------------------------------------------------------------------------ client
class RestFabricClient:
    """Implements :class:`~orqentis.fabric.ports.FabricClient` over the Fabric and Power BI REST APIs.

    Read-only by construction: there is no method that creates, updates, deletes, runs, cancels or
    refreshes anything. Mutations go through :class:`~orqentis.fabric.ports.FabricWriteClient`.
    """

    def __init__(
        self,
        *,
        fabric_base_url: str,
        powerbi_base_url: str,
        token_provider: TokenProvider,
        contracts: EstateContracts,
        tenant_id: str,
        timeout: float = 30.0,
        max_retries: int = 4,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Clock | None = None,
        budget: FabricCallBudget | None = None,
        job_instances_max_pages: int = 10,
        onelake_base_url: str = PUBLIC_ONELAKE_BASE_URL,
        onelake_scope: str = PUBLIC_STORAGE_SCOPE,
        delta_log_max_commit_bytes: int = DEFAULT_MAX_COMMIT_BYTES,
        monitoring_database_name: str = "",
        kusto_scope: str = PUBLIC_KUSTO_SCOPE,
        capacity_metrics_workspace_id: str | None = None,
        capacity_metrics_dataset_id: str | None = None,
    ) -> None:
        self._token_provider = token_provider
        self._http = FabricHttp(
            token_provider, timeout=timeout, max_retries=max_retries, transport=transport, sleep=sleep, budget=budget
        )
        self._environments = EnvironmentResolver(contracts.environments)
        ctx = _Context(self._http, fabric_base_url, powerbi_base_url, tenant_id, self._environments)
        self._environment_feed = ctx.feed
        self._workspaces = RestWorkspaceService(ctx)
        self._items = RestItemService(ctx, self._workspaces)
        self._jobs = RestJobService(ctx, self._workspaces, max_pages=job_instances_max_pages)
        self._pipelines = RestPipelineService(ctx, self._jobs, clock or SystemClock())
        self._semantic_models = RestSemanticModelService(ctx)
        metrics = (
            CapacityMetricsReader(
                self._http,
                powerbi_base_url=powerbi_base_url,
                workspace_id=capacity_metrics_workspace_id,
                dataset_id=capacity_metrics_dataset_id,
            )
            if capacity_metrics_workspace_id and capacity_metrics_dataset_id
            else None
        )
        self._capacities = RestCapacityService(ctx, metrics)
        self._monitoring = RestMonitoringService(
            self._http,
            fabric_base_url=fabric_base_url,
            tenant_id=tenant_id,
            database_name=monitoring_database_name,
            kusto_scope=kusto_scope,
        )
        self._delta_logs = RestDeltaLogService(
            self._http, base_url=onelake_base_url, scope=onelake_scope, max_commit_bytes=delta_log_max_commit_bytes
        )
        self._deployments = RestDeploymentService(ctx)
        self._dependencies = DeclaredDependencyService(contracts)

    @classmethod
    def from_settings(
        cls, settings: Settings, contracts: EstateContracts, token_provider: TokenProvider
    ) -> RestFabricClient:
        return cls(
            fabric_base_url=settings.fabric_api_base_url,
            powerbi_base_url=settings.powerbi_api_base_url,
            token_provider=token_provider,
            contracts=contracts,
            tenant_id=settings.tenant_id,
            timeout=settings.http_timeout_seconds,
            max_retries=settings.http_max_retries,
            budget=FabricCallBudget(
                max_concurrency=settings.fabric_max_concurrency,
                rate_per_second=settings.fabric_rate_per_second,
                burst=settings.fabric_rate_burst,
            ),
            job_instances_max_pages=settings.fabric_job_instances_max_pages,
            onelake_base_url=settings.onelake_base_url,
            onelake_scope=settings.onelake_scope,
            delta_log_max_commit_bytes=settings.delta_log_max_commit_bytes,
            monitoring_database_name=settings.workspace_monitoring_database,
            kusto_scope=settings.kusto_scope,
            capacity_metrics_workspace_id=settings.capacity_metrics_workspace_id,
            capacity_metrics_dataset_id=settings.capacity_metrics_dataset_id,
        )

    @property
    def workspaces(self) -> RestWorkspaceService:
        return self._workspaces

    @property
    def items(self) -> RestItemService:
        return self._items

    @property
    def jobs(self) -> RestJobService:
        return self._jobs

    @property
    def pipelines(self) -> RestPipelineService:
        return self._pipelines

    @property
    def semantic_models(self) -> RestSemanticModelService:
        return self._semantic_models

    @property
    def capacities(self) -> RestCapacityService:
        return self._capacities

    @property
    def deployments(self) -> RestDeploymentService:
        return self._deployments

    @property
    def dependencies(self) -> DeclaredDependencyService:
        return self._dependencies

    @property
    def monitoring(self) -> RestMonitoringService:
        return self._monitoring

    @property
    def delta_logs(self) -> RestDeltaLogService:
        return self._delta_logs

    def apply_contracts(self, contracts: EstateContracts) -> None:
        """Adopt reloaded contracts: declared environments and declared dependencies (the next listing uses them)."""
        self._environments.replace(contracts.environments)
        self._dependencies.replace(contracts)

    def observe_environment_changes(self, observer: EnvironmentObserver | None) -> None:
        """Be told, once per mapped node, when a contracts declaration changed its classification."""
        self._environment_feed.observer = observer

    async def aclose(self) -> None:
        try:
            await self._http.aclose()
        finally:
            close = getattr(self._token_provider, "close", None)
            if callable(close):
                result = close()
                if inspect.isawaitable(result):
                    await result


if TYPE_CHECKING:

    def _conforms_to_port(client: RestFabricClient) -> FabricClient:  # checked by mypy, never called
        return client
