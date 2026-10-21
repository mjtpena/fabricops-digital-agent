"""Read-only tools. None of these can change anything in Fabric."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from orqentis.config.clock import Clock
from orqentis.domain.common import ItemType
from orqentis.domain.estate import FabricItem
from orqentis.domain.events import EventType
from orqentis.domain.evidence import EvidenceType
from orqentis.domain.runs import CapacitySnapshot
from orqentis.domain.sla import SLAAssessment
from orqentis.fabric.ports import FabricClient
from orqentis.incidents.deployment import compare_deployments
from orqentis.incidents.history import IncidentHistory
from orqentis.incidents.sla import SLAEvaluator
from orqentis.runbooks.catalog import RunbookCatalog, RunbookQuery
from orqentis.storage.event_store import EventQuery, EventStore
from orqentis.tools.registry import ToolContext, ToolOutcome, ToolSpec
from orqentis.topology.catalog import EstateCatalog
from orqentis.topology.graph import DependencyGraph, GraphNode

_ID = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")

# How far back the refresh history tool looks in workspace monitoring (the database keeps 30 days).
_REFRESH_LOOKBACK = timedelta(days=14)


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


@dataclass
class ReadToolDependencies:
    fabric: FabricClient
    catalog: EstateCatalog
    graph: DependencyGraph
    event_store: EventStore
    history: IncidentHistory
    runbooks: RunbookCatalog
    sla: SLAEvaluator
    clock: Clock
    # Semantic model refresh outcomes come from workspace monitoring instead of the Power BI history (decision D9).
    refresh_from_monitoring: bool = False


def _workspace_for(deps: ReadToolDependencies, item_id: str) -> str:
    workspace_id = deps.catalog.workspace_of(item_id)
    if workspace_id is None:
        raise LookupError(f"Item {item_id} is not in the estate catalog")
    return workspace_id


def _hhmm(value: datetime | None) -> str:
    return value.isoformat(timespec="seconds") if value else "n/a"


# ----------------------------------------------------------------------------------------- inputs
class PipelineRunInput(_Input):
    pipeline_id: str = _ID
    run_id: str = _ID


class ItemInput(_Input):
    item_id: str = _ID


class WorkspaceInput(_Input):
    workspace_id: str = _ID


class DependencyInput(_Input):
    item_id: str = _ID
    max_depth: int | None = Field(default=None, ge=1, le=10)


class JobInstanceInput(_Input):
    item_id: str = _ID
    run_id: str = _ID


class JobInstancesInput(_Input):
    item_id: str = _ID
    limit: int = Field(default=10, ge=1, le=50)


class RefreshHistoryInput(_Input):
    semantic_model_id: str = _ID
    top: int = Field(default=10, ge=1, le=50)


class CapacityContextInput(_Input):
    workspace_id: str | None = Field(default=None, max_length=128)
    capacity_id: str | None = Field(default=None, max_length=128)
    around: datetime | None = Field(default=None, description="Centre of the window; defaults to now")
    window_minutes: int = Field(default=60, ge=5, le=24 * 60)

    @model_validator(mode="after")
    def _one_target(self) -> CapacityContextInput:
        if not (self.workspace_id or self.capacity_id):
            raise ValueError("workspace_id or capacity_id is required")
        return self


class RelatedIncidentsInput(_Input):
    item_id: str = _ID
    category: str | None = Field(default=None, max_length=64)
    error_text: str | None = Field(default=None, max_length=4000)
    limit: int = Field(default=5, ge=1, le=20)


class SearchRunbooksInput(_Input):
    category: str | None = Field(default=None, max_length=64)
    text: str | None = Field(default=None, max_length=8000)
    item_types: list[ItemType] = Field(default_factory=list, max_length=10)
    limit: int = Field(default=3, ge=1, le=10)


class EventsInput(_Input):
    item_ids: list[str] = Field(min_length=1, max_length=25)
    event_types: list[EventType] | None = None
    since: datetime | None = None
    until: datetime | None = None
    limit: int = Field(default=100, ge=1, le=500)


# ----------------------------------------------------------------------------------------- helpers
def _node_view(deps: ReadToolDependencies, node: GraphNode) -> dict[str, Any]:
    info = deps.catalog.node_or_placeholder(node.item_id)
    return {
        "item_id": node.item_id,
        "display_name": info.display_name,
        "item_type": info.item_type.value,
        "workspace_id": info.workspace_id,
        "environment": info.environment.value,
        "owner": info.owner,
        "depth": node.depth,
        "path": list(node.path),
        "via": {"edge_id": node.via.id, "dependency_type": node.via.dependency_type.value} if node.via else None,
    }


def summarize_capacity(snapshots: list[CapacitySnapshot]) -> dict[str, Any]:
    if not snapshots:
        return {"snapshots": 0, "throttling_detected": False}
    peak = max(snapshots, key=lambda s: s.cu_utilization_percent)
    workloads: dict[str, dict[str, Any]] = {}
    for snapshot in snapshots:
        for w in snapshot.top_workloads:
            entry = workloads.setdefault(
                w.item_id,
                {
                    "item_id": w.item_id,
                    "display_name": w.display_name,
                    "workspace_id": w.workspace_id,
                    "item_type": w.item_type.value,
                    "peak_cu_percent": 0.0,
                    "samples": 0,
                },
            )
            entry["peak_cu_percent"] = max(entry["peak_cu_percent"], w.cu_percent)
            entry["samples"] += 1
    throttled = [s for s in snapshots if s.is_throttling or s.cu_utilization_percent >= 100]
    return {
        "snapshots": len(snapshots),
        "window_start": snapshots[0].timestamp.isoformat(),
        "window_end": snapshots[-1].timestamp.isoformat(),
        "peak_cu_percent": peak.cu_utilization_percent,
        "peak_at": peak.timestamp.isoformat(),
        "max_interactive_delay_percent": max(s.interactive_delay_percent for s in snapshots),
        "max_background_rejection_percent": max(s.background_rejection_percent for s in snapshots),
        "throttling_detected": bool(throttled),
        "throttling_start": throttled[0].timestamp.isoformat() if throttled else None,
        "throttling_end": throttled[-1].timestamp.isoformat() if throttled else None,
        "top_workloads": sorted(workloads.values(), key=lambda w: -float(w["peak_cu_percent"])),
    }


async def assess_slas_downstream(
    deps: ReadToolDependencies, item_id: str, *, blocking_item_ids: list[str]
) -> list[SLAAssessment]:
    """SLAs of the data products at or downstream of ``item_id``."""
    candidates = {item_id, *(n.item_id for n in deps.graph.get_downstream(item_id))}
    assessments: list[SLAAssessment] = []
    for dp_id in sorted(c for c in candidates if c in deps.catalog.data_products):
        data_product = deps.catalog.data_products[dp_id]
        for sla in deps.catalog.slas_for_data_product(dp_id):
            assessments.append(
                await deps.sla.assess(
                    sla,
                    data_product_name=data_product.display_name,
                    blocking_item_ids=blocking_item_ids,
                )
            )
    return assessments


# ----------------------------------------------------------------------------------------- tools
def build_read_tools(deps: ReadToolDependencies) -> list[ToolSpec]:
    async def get_pipeline_run(p: PipelineRunInput, ctx: ToolContext) -> ToolOutcome:
        ws = _workspace_for(deps, p.pipeline_id)
        run = await deps.fabric.jobs.get_job_instance(ws, p.pipeline_id, p.run_id)
        failure = f": {run.failure.message}" if run.failure else ""
        return ToolOutcome(
            data=run,
            source=run.source,
            resource_id=run.item_id,
            resource_type=ItemType.DATA_PIPELINE.value,
            observed_at=run.end_time or run.start_time,
            summary=f"Run {run.id} of {run.display_name or run.item_id} {run.status.value} "
            f"({_hhmm(run.start_time)} to {_hhmm(run.end_time)}){failure}",
        )

    async def get_pipeline_activity_runs(p: PipelineRunInput, ctx: ToolContext) -> ToolOutcome:
        ws = _workspace_for(deps, p.pipeline_id)
        runs = await deps.fabric.pipelines.query_activity_runs(ws, p.pipeline_id, p.run_id)
        parts = [f"{a.activity_name} {a.status.value}" for a in runs]
        return ToolOutcome(
            data=runs,
            source="fabric.pipelines",
            resource_id=p.pipeline_id,
            resource_type=ItemType.DATA_PIPELINE.value,
            summary=f"{len(runs)} activity run(s) for {p.run_id}: " + ", ".join(parts),
        )

    async def get_job_instance(p: JobInstanceInput, ctx: ToolContext) -> ToolOutcome:
        ws = _workspace_for(deps, p.item_id)
        run = await deps.fabric.jobs.get_job_instance(ws, p.item_id, p.run_id)
        item = deps.catalog.items.get(p.item_id)
        name = item.display_name if item else p.item_id
        failure = f": {run.failure.message}" if run.failure else ""
        return ToolOutcome(
            data=run,
            source=run.source,
            resource_id=run.item_id,
            resource_type=item.item_type.value if item else None,
            observed_at=run.end_time or run.start_time,
            summary=f"Job {run.id} of {name} {run.status.value} ({_hhmm(run.start_time)} to {_hhmm(run.end_time)})"
            f"{failure}",
        )

    async def get_job_instances(p: JobInstancesInput, ctx: ToolContext) -> ToolOutcome:
        ws = _workspace_for(deps, p.item_id)
        runs = await deps.fabric.jobs.list_job_instances(ws, p.item_id, limit=p.limit)
        item = deps.catalog.items.get(p.item_id)
        name = item.display_name if item else p.item_id
        succeeded = [r for r in runs if r.status.value == "Succeeded"]
        last = (succeeded[0].end_time or succeeded[0].start_time) if succeeded else None
        failed = sum(1 for r in runs if r.status.is_failure)
        return ToolOutcome(
            data=runs,
            source="fabric.jobs",
            resource_id=p.item_id,
            resource_type=item.item_type.value if item else None,
            observed_at=last,
            summary=f"{len(runs)} recent run(s) of {name}; last success {_hhmm(last)}; {failed} failure(s)",
        )

    async def get_item(p: ItemInput, ctx: ToolContext) -> ToolOutcome:
        if (dp := deps.catalog.data_products.get(p.item_id)) is not None:
            return ToolOutcome(
                data=dp,
                source="declared-contracts",
                resource_id=dp.id,
                resource_type=ItemType.DATA_PRODUCT.value,
                summary=f"Data product {dp.display_name} owned by {dp.owner}",
            )
        cataloged = deps.catalog.items.get(p.item_id)
        if cataloged is None:
            raise LookupError(f"Item {p.item_id} is not in the estate catalog")
        item: FabricItem
        if cataloged.item_type is ItemType.EXTERNAL_SOURCE:
            item = cataloged
        else:
            item = await deps.fabric.items.get_item(cataloged.workspace_id, p.item_id)
        return ToolOutcome(
            data=item,
            source=item.source,
            resource_id=item.id,
            resource_type=item.item_type.value,
            summary=f"{item.item_type.value} {item.display_name} in {item.workspace_id} ({item.environment.value}), "
            f"owner {item.owner or 'unknown'}",
        )

    async def get_workspace(p: WorkspaceInput, ctx: ToolContext) -> ToolOutcome:
        ws = await deps.fabric.workspaces.get_workspace(p.workspace_id)
        capacity = deps.catalog.capacities.get(ws.capacity_id) if ws.capacity_id else None
        return ToolOutcome(
            data={"workspace": ws, "capacity": capacity},
            source=ws.source,
            resource_id=ws.id,
            resource_type="Workspace",
            summary=f"Workspace {ws.display_name} ({ws.environment.value}) on capacity "
            f"{capacity.display_name if capacity else ws.capacity_id}",
        )

    async def get_upstream_dependencies(p: DependencyInput, ctx: ToolContext) -> ToolOutcome:
        nodes = deps.graph.get_upstream(p.item_id, max_depth=p.max_depth)
        names = ", ".join(deps.catalog.node_or_placeholder(n.item_id).display_name for n in nodes) or "none"
        return ToolOutcome(
            data=[_node_view(deps, n) for n in nodes],
            source="orqentis.topology",
            resource_id=p.item_id,
            summary=f"{len(nodes)} upstream of {p.item_id}: {names}",
        )

    async def get_downstream_dependencies(p: DependencyInput, ctx: ToolContext) -> ToolOutcome:
        nodes = deps.graph.get_downstream(p.item_id, max_depth=p.max_depth)
        names = ", ".join(deps.catalog.node_or_placeholder(n.item_id).display_name for n in nodes) or "none"
        return ToolOutcome(
            data=[_node_view(deps, n) for n in nodes],
            source="orqentis.topology",
            resource_id=p.item_id,
            summary=f"{len(nodes)} downstream of {p.item_id}: {names}",
        )

    async def get_semantic_model_refresh_history(p: RefreshHistoryInput, ctx: ToolContext) -> ToolOutcome:
        ws = _workspace_for(deps, p.semantic_model_id)
        if deps.refresh_from_monitoring:
            # The reader cannot read the Power BI history (dataset Write); the workspace's monitoring database has it.
            now = deps.clock.now()
            logged = await deps.fabric.monitoring.list_semantic_model_refreshes(
                ws, since=now - _REFRESH_LOOKBACK, until=now
            )
            refreshes = [r for r in logged if r.item_id == p.semantic_model_id][: p.top]
            source = "fabric.workspace-monitoring"
        else:
            refreshes = await deps.fabric.semantic_models.get_refresh_history(ws, p.semantic_model_id, top=p.top)
            source = "powerbi.refreshes"
        succeeded = [r for r in refreshes if r.status.value == "Succeeded"]
        last = succeeded[0].end_time if succeeded else None
        failed = sum(1 for r in refreshes if r.status.is_failure)
        return ToolOutcome(
            data=refreshes,
            source=source,
            resource_id=p.semantic_model_id,
            resource_type=ItemType.SEMANTIC_MODEL.value,
            observed_at=last,
            summary=f"{len(refreshes)} refresh(es); last success {_hhmm(last)}; {failed} failure(s)",
        )

    async def get_capacity_context(p: CapacityContextInput, ctx: ToolContext) -> ToolOutcome:
        capacity_id = p.capacity_id
        if capacity_id is None and p.workspace_id:
            capacity = deps.catalog.capacity_of_workspace(p.workspace_id)
            if capacity is None:
                raise LookupError(f"No capacity assignment known for workspace {p.workspace_id}")
            capacity_id = capacity.id
        assert capacity_id is not None
        capacity_obj = deps.catalog.capacities.get(capacity_id)
        around = p.around or deps.clock.now()
        start = around - timedelta(minutes=p.window_minutes)
        end = min(around + timedelta(minutes=p.window_minutes // 4), deps.clock.now())
        snapshots = await deps.fabric.capacities.get_capacity_metrics(capacity_id, start=start, end=end)
        stats = summarize_capacity(snapshots)
        sharing = [
            {"workspace_id": w.id, "display_name": w.display_name, "environment": w.environment.value}
            for w in deps.catalog.workspaces.values()
            if w.capacity_id == capacity_id
        ]
        top = stats.get("top_workloads") or []
        top_text = (
            f"; top workload {top[0]['display_name']} ({top[0]['workspace_id']}) peak {top[0]['peak_cu_percent']:.0f}%"
            if top
            else ""
        )
        summary = (
            f"Capacity {capacity_obj.display_name if capacity_obj else capacity_id}: "
            f"peak {stats.get('peak_cu_percent', 0):.0f}% CU, background rejection up to "
            f"{stats.get('max_background_rejection_percent', 0):.0f}%, interactive delay up to "
            f"{stats.get('max_interactive_delay_percent', 0):.0f}%{top_text}"
            if snapshots
            else f"No capacity metrics available for {capacity_id} in the window"
        )
        return ToolOutcome(
            data={
                "capacity": capacity_obj,
                "workspaces_on_capacity": sharing,
                "statistics": stats,
                "snapshots": snapshots,
            },
            source="fabric.capacity-metrics",
            resource_id=capacity_id,
            resource_type="Capacity",
            observed_at=snapshots[-1].timestamp if snapshots else None,
            summary=summary,
        )

    async def get_recent_related_incidents(p: RelatedIncidentsInput, ctx: ToolContext) -> ToolOutcome:
        before = ctx.incident.detected_at if ctx.incident else deps.clock.now()
        matches = await deps.history.search_related(
            primary_item_id=p.item_id,
            before=before,
            exclude_incident_id=ctx.incident.incident_id if ctx.incident else None,
            category_hint=p.category,
            error_text=p.error_text,
            limit=p.limit,
        )
        listing = ", ".join(f"{m.incident_id} ({m.similarity:.2f})" for m in matches) or "none"
        return ToolOutcome(
            data=matches,
            source="orqentis.incident-history",
            resource_id=p.item_id,
            summary=f"{len(matches)} related incident(s): {listing}",
        )

    async def search_runbooks(p: SearchRunbooksInput, ctx: ToolContext) -> ToolOutcome:
        matches = deps.runbooks.search(
            RunbookQuery(category=p.category, text=p.text, item_types=p.item_types, limit=p.limit)
        )
        listing = ", ".join(f"{m.runbook_id} v{m.version} ({m.score:.2f})" for m in matches) or "none"
        return ToolOutcome(data=matches, source="orqentis.runbooks", summary=f"Runbook matches: {listing}")

    async def get_deployment_state(p: ItemInput, ctx: ToolContext) -> ToolOutcome:
        states = await deps.fabric.deployments.get_deployment_states(p.item_id)
        reference = ctx.incident.detected_at if ctx.incident else deps.clock.now()
        comparison = compare_deployments(p.item_id, states, reference_time=reference)
        if comparison is None:
            return ToolOutcome(
                data={"states": [], "comparison": None},
                source="fabric.deployment-pipelines",
                resource_id=p.item_id,
                summary=f"No deployment state recorded for {p.item_id}",
            )
        recent = "changed" if comparison.changed_within_window else "not changed"
        drift = "drift detected" if comparison.drift_detected else "no TEST/PROD drift"
        return ToolOutcome(
            data={"states": states, "comparison": comparison},
            source="fabric.deployment-pipelines",
            resource_id=p.item_id,
            observed_at=comparison.last_production_change,
            summary=f"{comparison.logical_item_id}: {drift}; production {recent} in the "
            f"{comparison.window_hours}h before the incident (last change "
            f"{_hhmm(comparison.last_production_change)})",
        )

    async def get_sla(p: ItemInput, ctx: ToolContext) -> ToolOutcome:
        # An open incident on this item (or upstream of it) blocks today's refresh.
        incident = ctx.incident
        blocking = (
            [incident.primary_item_id]
            if incident is not None
            and incident.status.is_open
            and (
                p.item_id == incident.primary_item_id
                or deps.graph.find_path(incident.primary_item_id, p.item_id) is not None
            )
            else []
        )
        assessments = await assess_slas_downstream(deps, p.item_id, blocking_item_ids=blocking)
        listing = ", ".join(f"{a.data_product_name} {a.state.value}" for a in assessments) or "no SLAs"
        return ToolOutcome(
            data=assessments,
            source="orqentis.sla",
            resource_id=p.item_id,
            summary=f"SLA status downstream of {p.item_id}: {listing}",
        )

    async def get_operational_events(p: EventsInput, ctx: ToolContext) -> ToolOutcome:
        events = await deps.event_store.query(
            EventQuery(item_ids=p.item_ids, event_types=p.event_types, since=p.since, until=p.until, limit=p.limit)
        )
        return ToolOutcome(
            data=events,
            source="orqentis.event-store",
            summary=f"{len(events)} operational event(s) for {', '.join(p.item_ids)}",
        )

    return [
        ToolSpec(
            "get_pipeline_run",
            "Get one pipeline run (job instance) by pipeline id and run id.",
            PipelineRunInput,
            get_pipeline_run,
            evidence_type=EvidenceType.PIPELINE_FAILURE,
        ),
        ToolSpec(
            "get_pipeline_activity_runs",
            "List the activity runs (and their errors) of a pipeline run.",
            PipelineRunInput,
            get_pipeline_activity_runs,
            evidence_type=EvidenceType.ACTIVITY_FAILURE,
        ),
        ToolSpec(
            "get_job_instance",
            "Get one job instance (a notebook, Spark job definition or Dataflow run) by item id and run id.",
            JobInstanceInput,
            get_job_instance,
            evidence_type=EvidenceType.JOB_FAILURE,
        ),
        ToolSpec(
            "get_job_instances",
            "List the most recent job instances of an item, newest first.",
            JobInstancesInput,
            get_job_instances,
            evidence_type=EvidenceType.RUN_HISTORY,
        ),
        ToolSpec(
            "get_item",
            "Get metadata of a Fabric item, declared external source, or data product.",
            ItemInput,
            get_item,
            evidence_type=EvidenceType.ITEM_METADATA,
        ),
        ToolSpec(
            "get_workspace",
            "Get workspace metadata including its capacity assignment.",
            WorkspaceInput,
            get_workspace,
            evidence_type=EvidenceType.WORKSPACE_METADATA,
        ),
        ToolSpec(
            "get_upstream_dependencies",
            "List items upstream of an item in the dependency graph.",
            DependencyInput,
            get_upstream_dependencies,
            evidence_type=EvidenceType.DEPENDENCY_EDGE,
        ),
        ToolSpec(
            "get_downstream_dependencies",
            "List items downstream of an item in the dependency graph.",
            DependencyInput,
            get_downstream_dependencies,
            evidence_type=EvidenceType.DEPENDENCY_EDGE,
        ),
        ToolSpec(
            "get_semantic_model_refresh_history",
            "Get the refresh history of a semantic model.",
            RefreshHistoryInput,
            get_semantic_model_refresh_history,
            evidence_type=EvidenceType.SEMANTIC_MODEL_REFRESH,
        ),
        ToolSpec(
            "get_capacity_context",
            "Get capacity utilisation, throttling and top workloads around a time.",
            CapacityContextInput,
            get_capacity_context,
            evidence_type=EvidenceType.CAPACITY_METRIC,
        ),
        ToolSpec(
            "get_recent_related_incidents",
            "Search previous incidents similar to the current failure.",
            RelatedIncidentsInput,
            get_recent_related_incidents,
            evidence_type=EvidenceType.HISTORICAL_INCIDENT,
        ),
        ToolSpec(
            "search_runbooks",
            "Retrieve version-controlled runbooks matching a category and error text.",
            SearchRunbooksInput,
            search_runbooks,
            evidence_type=EvidenceType.RUNBOOK_MATCH,
        ),
        ToolSpec(
            "get_deployment_state",
            "Compare an item's deployment state across DEV/TEST/PROD.",
            ItemInput,
            get_deployment_state,
            evidence_type=EvidenceType.DEPLOYMENT_COMPARISON,
        ),
        ToolSpec(
            "get_sla",
            "Evaluate SLAs of data products at or downstream of an item.",
            ItemInput,
            get_sla,
            evidence_type=EvidenceType.SLA_STATUS,
        ),
        ToolSpec(
            "get_operational_events",
            "Query normalised operational events for items.",
            EventsInput,
            get_operational_events,
            evidence_type=EvidenceType.OPERATIONAL_EVENT,
        ),
    ]
