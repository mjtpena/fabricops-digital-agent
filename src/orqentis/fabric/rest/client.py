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
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx

from orqentis.config.clock import Clock, SystemClock
from orqentis.config.settings import Settings
from orqentis.domain.common import Environment, ItemType
from orqentis.domain.deployment import DeploymentState
from orqentis.domain.estate import (
    Capacity,
    FabricItem,
    GenericItem,
    Lakehouse,
    Notebook,
    Pipeline,
    Report,
    SemanticModel,
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
from orqentis.fabric.fixtures import EstateContracts
from orqentis.fabric.ports import FabricClient
from orqentis.fabric.rest.auth import FABRIC_SCOPE, POWERBI_SCOPE, TokenProvider
from orqentis.fabric.rest.http import FabricHttp, JsonObject, strip_privacy_tags
from orqentis.observability.logging import get_logger

_log = get_logger(__name__)

# ------------------------------------------------------------------------------------ vocabularies
_PROD_TOKENS = frozenset({"prod", "production", "prd"})
_TEST_TOKENS = frozenset({"test", "testing", "tst", "uat", "qa", "sit", "preprod", "staging", "stg"})
_DEV_TOKENS = frozenset({"dev", "development", "develop"})

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
_JOB_ITEM_TYPES = {"Pipeline": ItemType.DATA_PIPELINE, "RunNotebook": ItemType.NOTEBOOK}

_ItemClass = type[Pipeline | Notebook | Lakehouse | Warehouse | SemanticModel | Report]
_ITEM_CLASSES: dict[str, _ItemClass] = {
    ItemType.DATA_PIPELINE.value: Pipeline,
    ItemType.NOTEBOOK.value: Notebook,
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
def infer_environment(name: str | None) -> Environment:
    """Environment from naming conventions such as ``Sales [PROD]``, ``sales-prod`` or ``Sales UAT``.

    The last recognised token wins (suffix convention). ``Non-Prod`` / ``Pre-Prod`` are not PROD.
    """
    if not name:
        return Environment.UNKNOWN
    tokens: list[str] = []
    for token in re.findall(r"[a-z0-9]+", name.lower()):
        if tokens and tokens[-1] in ("non", "pre") and token in _PROD_TOKENS:
            tokens[-1] += "prod"
        else:
            tokens.append(token)
    for token in reversed(tokens):
        if token in _PROD_TOKENS:
            return Environment.PROD
        if token in _TEST_TOKENS:
            return Environment.TEST
        if token in _DEV_TOKENS:
            return Environment.DEV
    return Environment.UNKNOWN


def stage_environment(name: str | None, index: int, count: int) -> Environment:
    """Stage environment: an explicit name wins, else first=DEV, last=PROD, anything between=TEST."""
    named = infer_environment(name)
    if named is not Environment.UNKNOWN or count <= 1:
        return named
    if index == 0:
        return Environment.DEV
    if index == count - 1:
        return Environment.PROD
    return Environment.TEST


def parse_datetime(value: Any) -> datetime | None:
    """Fabric/Power BI timestamps (``Z`` suffix, 7 fractional digits, or naive UTC) as aware UTC datetimes."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if parsed.year <= 1:  # .NET DateTime.MinValue placeholder for "not set"
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


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


def _segment(value: str) -> str:
    if not value or value in (".", ".."):
        raise FabricError(f"Invalid Fabric identifier {value!r}", error_code="InvalidIdentifier")
    return quote(value, safe="")


@dataclass(frozen=True)
class _Context:
    """What every service needs: the HTTP client, the API base URLs and the tenant id."""

    http: FabricHttp
    fabric_base_url: str
    powerbi_base_url: str
    tenant_id: str

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
        workspace = Workspace(
            id=_required(raw, "id", "workspace"),
            tenant_id=self._ctx.tenant_id,
            display_name=display_name,
            capacity_id=_clean(raw.get("capacityId")),
            environment=infer_environment(display_name),
            description=_clean(raw.get("description")),
            metadata=_compact(workspace_type=_clean(raw.get("type")), domain_id=_clean(raw.get("domainId"))),
        )
        self._environments[workspace.id] = workspace.environment
        return workspace

    async def list_workspaces(self) -> list[Workspace]:
        raw = await self._ctx.http.paginate(self._ctx.fabric("workspaces"), scope=FABRIC_SCOPE)
        return [self._map(entry) for entry in raw]

    async def get_workspace(self, workspace_id: str) -> Workspace:
        raw = await self._ctx.http.get_json(self._ctx.fabric("workspaces", workspace_id), scope=FABRIC_SCOPE)
        return self._map(raw)

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
        fields: dict[str, Any] = {
            "id": _required(raw, "id", "item"),
            "tenant_id": self._ctx.tenant_id,
            "workspace_id": _clean(raw.get("workspaceId")) or workspace_id,
            "display_name": _required(raw, "displayName", "item"),
            "description": _clean(raw.get("description")),
            "environment": environment,
            "tags": tags,
            "metadata": _compact(folder_id=_clean(raw.get("folderId"))),
        }
        item_class = _ITEM_CLASSES.get(fabric_type or "")
        if item_class is not None:
            return item_class(**fields)
        return GenericItem(**fields, fabric_type=fabric_type)

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
        return [i for i in items if item_type is None or i.item_type == item_type]

    async def get_item(self, workspace_id: str, item_id: str) -> FabricItem:
        raw = await self._ctx.http.get_json(
            self._ctx.fabric("workspaces", workspace_id, "items", item_id), scope=FABRIC_SCOPE
        )
        return self._map(raw, workspace_id, await self._workspaces.environment_of(workspace_id))


class RestJobService:
    def __init__(self, ctx: _Context, workspaces: RestWorkspaceService) -> None:
        self._ctx = ctx
        self._workspaces = workspaces

    def _map(self, raw: JsonObject, workspace_id: str, item_id: str) -> PipelineRun:
        job_type, fabric_status = _clean(raw.get("jobType")), _clean(raw.get("status"))
        return PipelineRun(
            id=_required(raw, "id", "job instance"),
            tenant_id=self._ctx.tenant_id,
            workspace_id=workspace_id,
            item_id=_clean(raw.get("itemId")) or item_id,
            item_type=_JOB_ITEM_TYPES.get(job_type or "Pipeline", ItemType.OTHER),
            environment=self._workspaces.known_environment(workspace_id),
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

    async def list_job_instances(self, workspace_id: str, item_id: str, *, limit: int = 50) -> list[PipelineRun]:
        if limit <= 0:
            return []
        url = self._instances_url(workspace_id, item_id)
        runs = [self._map(raw, workspace_id, item_id) for raw in await self._ctx.http.paginate(url, scope=FABRIC_SCOPE)]
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
    def __init__(self, ctx: _Context) -> None:
        self._ctx = ctx

    async def list_capacities(self) -> list[Capacity]:
        capacities: list[Capacity] = []
        for raw in await self._ctx.http.paginate(self._ctx.fabric("capacities"), scope=FABRIC_SCOPE):
            display_name = _required(raw, "displayName", "capacity")
            capacities.append(
                Capacity(
                    id=_required(raw, "id", "capacity"),
                    tenant_id=self._ctx.tenant_id,
                    display_name=display_name,
                    sku=_clean(raw.get("sku")) or "Unknown",
                    region=_clean(raw.get("region")),
                    state=_clean(raw.get("state")) or "Unknown",
                    environment=infer_environment(display_name),
                )
            )
        return capacities

    async def get_capacity_metrics(self, capacity_id: str, *, start: datetime, end: datetime) -> list[CapacitySnapshot]:
        raise FabricCapabilityUnavailableError(
            "Capacity CU utilisation is not exposed by any public Fabric REST API. Ingest the Fabric "
            "Capacity Metrics data into the Eventhouse event store and query it from there instead.",
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
                    environment=stage_environment(_clean(s.get("displayName")), position, len(ordered)),
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
    ) -> None:
        self._token_provider = token_provider
        self._http = FabricHttp(
            token_provider, timeout=timeout, max_retries=max_retries, transport=transport, sleep=sleep
        )
        ctx = _Context(self._http, fabric_base_url, powerbi_base_url, tenant_id)
        self._workspaces = RestWorkspaceService(ctx)
        self._items = RestItemService(ctx, self._workspaces)
        self._jobs = RestJobService(ctx, self._workspaces)
        self._pipelines = RestPipelineService(ctx, self._jobs, clock or SystemClock())
        self._semantic_models = RestSemanticModelService(ctx)
        self._capacities = RestCapacityService(ctx)
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
