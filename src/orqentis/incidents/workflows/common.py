"""Pipeline failure investigation: deterministic orchestration around the FabricOps Investigator.

The numbered steps mirror the product specification. Every tool call goes through the tool
registry (logged, audited, traced) and every fact used later is recorded as evidence.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from orqentis.actions.gateway import ActionGateway, GatewayStatus
from orqentis.agents.investigator import FabricOpsInvestigator
from orqentis.agents.models import (
    CapacityObservation,
    ContendingWorkload,
    CredentialObservation,
    DeploymentObservation,
    ErrorObservation,
    EvidencePackage,
    InvestigationContext,
    PrimaryItemView,
    SchemaChangeObservation,
    UpstreamIncidentObservation,
)
from orqentis.approvals.service import ApprovalService
from orqentis.config.clock import Clock
from orqentis.domain.actions import Action, ActionType, PolicyEffect, Recommendation, RiskLevel
from orqentis.domain.common import Environment, ItemType, new_id
from orqentis.domain.deployment import DeploymentComparison
from orqentis.domain.events import EventType, OperationalEvent
from orqentis.domain.evidence import Evidence, EvidenceType, Finding
from orqentis.domain.incidents import (
    ApprovalState,
    BlastRadius,
    Incident,
    IncidentStatus,
    RelatedIncidentMatch,
    RootCauseCandidate,
    TimelineKind,
)
from orqentis.domain.principals import AGENT_PRINCIPAL, SYSTEM_PRINCIPAL
from orqentis.domain.runbooks import ActionTarget, Runbook, RunbookMatch
from orqentis.domain.runs import ActivityRun, PipelineRun, RunStatus
from orqentis.domain.sla import SLAAssessment, SLAState
from orqentis.domain.taxonomy import IncidentCategory, Severity
from orqentis.evidence.recorder import EvidenceRecorder
from orqentis.incidents.classification import ErrorClassifier, SignatureMatch
from orqentis.incidents.confidence import ConfidenceCalculator, ConfidenceSignals, Signal, corroboration_value
from orqentis.incidents.history import IncidentHistory
from orqentis.incidents.service import IncidentService
from orqentis.incidents.workflows.base import InvestigationTrace
from orqentis.policies.engine import PolicyEngine
from orqentis.reports.publisher import ReportPublisher
from orqentis.runbooks.catalog import RunbookCatalog
from orqentis.storage.evidence_store import EvidenceStore
from orqentis.storage.incidents import IncidentQuery
from orqentis.tools.models import ToolResponse, ToolStatus
from orqentis.tools.registry import ToolContext, ToolRegistry
from orqentis.topology.catalog import EstateCatalog
from orqentis.topology.graph import DependencyGraph

CAUSAL_WINDOW = timedelta(hours=24)
SNAPSHOT_GRANULARITY = timedelta(minutes=10)
SKIPPED_STATUSES = {RunStatus.SKIPPED, RunStatus.NOT_STARTED, RunStatus.CANCELLED}
_CODE_ITEMS = {ItemType.DATA_PIPELINE, ItemType.NOTEBOOK}


@dataclass
class WorkflowDependencies:
    registry: ToolRegistry
    incidents: IncidentService
    evidence_store: EvidenceStore
    investigator: FabricOpsInvestigator
    classifier: ErrorClassifier
    catalog: EstateCatalog
    graph: DependencyGraph
    history: IncidentHistory
    runbooks: RunbookCatalog
    confidence: ConfidenceCalculator
    policy: PolicyEngine
    gateway: ActionGateway
    approvals: ApprovalService
    reports: ReportPublisher
    clock: Clock
    auto_request_approval: bool = True


@dataclass
class WorkflowState:
    """Mutable working state of one investigation run (what a workflow observed so far)."""

    incident: Incident
    ctx: ToolContext
    recorder: EvidenceRecorder
    trace: InvestigationTrace
    first_evidence_index: int
    failure_time: datetime
    run: PipelineRun | None = None
    activities: list[ActivityRun] = field(default_factory=list)
    analysis: InvestigationContext | None = None
    upstream_ids: set[str] = field(default_factory=set)
    downstream_ids: set[str] = field(default_factory=set)
    skipped_ids: list[str] = field(default_factory=list)
    # What a workflow keeps between its own steps (never read by the shared ones).
    scratch: dict[str, Any] = field(default_factory=dict)

    @property
    def a(self) -> InvestigationContext:
        assert self.analysis is not None
        return self.analysis

    def start_analysis(self) -> InvestigationContext:
        """Open the deterministic analysis once the failure time is known (after the first observation)."""
        self.analysis = InvestigationContext(failure_time=self.failure_time)
        return self.analysis


def _iso(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


SLA_RULE_ID = "DET-SLA-BREACHED"


def is_sla_incident(incident: Incident) -> bool:
    """Opened by the SLA breach rule: the SLA workflow investigates it whatever kind of item refreshes the product."""
    return incident.metadata.get("detection_rule") == SLA_RULE_ID


class DiagnosticWorkflow:
    """The steps every investigation shares; a subclass supplies what it observes (steps 2 to 4).

    The numbered steps mirror the product specification. Every tool call goes through the tool registry (logged,
    audited, traced) and every fact used later is recorded as evidence. A subclass sets ``name`` and ``noun`` (used in
    step names), says which incidents it ``supports`` and implements :meth:`_observe`: the three steps that fetch the
    failure itself, open the analysis (``st.start_analysis()``) and record the error evidence. Everything after that,
    from item metadata to the report, is shared and identical for every kind of incident.
    """

    name = "diagnostic-v1"
    noun = "item"

    def __init__(self, deps: WorkflowDependencies) -> None:
        self._d = deps

    def supports(self, incident: Incident) -> bool:
        raise NotImplementedError

    async def _observe(self, st: WorkflowState) -> None:
        """Steps 2 to 4: fetch the failure, open the analysis, record the error evidence."""
        raise NotImplementedError

    async def _after_upstream(self, st: WorkflowState) -> None:
        """Extra upstream analysis of a workflow (inside the upstream step); the default adds nothing."""
        return None

    async def run(self, incident: Incident, *, trace: InvestigationTrace) -> Incident:
        d = self._d
        clock = d.clock
        recorder = EvidenceRecorder(incident, d.evidence_store, clock)
        st = WorkflowState(
            incident=incident,
            ctx=ToolContext(
                principal=SYSTEM_PRINCIPAL,
                trace_id=trace.trace_id,
                incident=incident,
                recorder=recorder,
                call_log=trace.tool_calls,
            ),
            recorder=recorder,
            trace=trace,
            first_evidence_index=len(incident.evidence),
            failure_time=incident.detected_at,
        )

        with trace.step(1, "Create incident / start investigation", clock):
            if incident.status is IncidentStatus.AWAITING_APPROVAL:
                await d.approvals.cancel_pending(
                    incident.incident_id, actor=SYSTEM_PRINCIPAL.id, reason="Superseded by re-investigation"
                )
            incident.agent_trace_id = trace.trace_id
            self._reset_analysis(incident)
            await d.incidents.transition(
                incident,
                IncidentStatus.INVESTIGATING,
                actor=SYSTEM_PRINCIPAL.id,
                reason=f"Investigation {trace.trace_id} started ({self.name})",
            )

        await self._observe(st)  # steps 2 to 4
        if st.analysis is None:  # nothing could be fetched: the analysis still opens, at the detection time
            st.start_analysis()

        with trace.step(5, f"Resolve {self.noun} metadata", clock):
            await self._resolve_metadata(st)
        with trace.step(6, "Traverse upstream dependencies", clock):
            await self._upstream(st)
            await self._after_upstream(st)
        with trace.step(7, "Traverse downstream dependencies", clock):
            await self._downstream(st)
        with trace.step(8, "Check affected semantic models", clock):
            await self._semantic_models(st)
        with trace.step(9, "Determine data-product SLA impact", clock):
            await self._sla(st)
        with trace.step(10, "Query capacity context", clock):
            await self._capacity(st)
        with trace.step(11, "Search previous incidents and deployment history", clock):
            await self._history(st)
            await self._deployments(st)
        with trace.step(12, "Retrieve matching runbooks", clock):
            await self._runbooks(st)

        with trace.step(13, "Provide evidence package to agent", clock) as step:
            package = self._package(st)
            step.note = f"{len(package.evidence)} evidence item(s)"
        with trace.step(14, "Generate findings (FabricOps Investigator)", clock) as step:
            result = await d.investigator.investigate(
                package, incident=incident, recorder=recorder, trace_id=trace.trace_id
            )
            trace.agent_decision = result.decision
            incident.agent_decision = result.decision
            step.note = (
                f"reasoner={result.decision.reasoner}; accepted={len(result.grounded.findings)}; "
                f"rejected={len(result.grounded.rejected)}"
            )
        grounded = result.grounded
        with trace.step(15, "Generate candidate root cause", clock):
            incident.findings = grounded.findings
            incident.insufficient_evidence_reason = grounded.insufficient_evidence_reason
            for finding in grounded.findings:
                d.incidents.add_timeline(
                    incident,
                    TimelineKind.FINDING,
                    f"{finding.category.value}: {finding.statement[:160]}",
                    actor=result.decision.agent_name,
                    evidence_ids=finding.evidence_ids,
                )
        with trace.step(16, "Calculate confidence", clock):
            self._confidence(st, grounded.root_cause, grounded.root_cause_category, grounded.runbook_id)
        with trace.step(17, "Generate remediation recommendation", clock):
            recommendation = self._recommendation(st, grounded.runbook_id, grounded.root_cause)
            incident.recommended_actions = [recommendation]
        with trace.step(18, "Evaluate remediation policy", clock):
            await self._policy(st, recommendation)
        with trace.step(19, "Persist incident", clock):
            await d.incidents.transition(
                incident,
                IncidentStatus.DIAGNOSED,
                actor=AGENT_PRINCIPAL.id,
                reason=(
                    f"Root cause {incident.category} ({incident.confidence.level.value})"
                    if incident.probable_root_causes and incident.confidence
                    else "Root cause undetermined"
                ),
            )
            if incident.approval_state is ApprovalState.PENDING:
                await d.incidents.transition(
                    incident,
                    IncidentStatus.AWAITING_APPROVAL,
                    actor="policy-engine",
                    reason=f"{len(incident.approval_ids)} action(s) require approval",
                )
        with trace.step(20, "Render Incident Report", clock):
            _, uri = await d.reports.publish(incident)
            incident.report_uri = uri
            await d.incidents.save(incident)
        return incident

    # ------------------------------------------------------------------------------------------ helpers
    async def _record_upstream_incidents(self, st: WorkflowState) -> None:
        """Incidents already open at, or upstream of, the failing item when it failed: evidence and context.

        A failure with an open incident behind it usually has that incident as its cause (a model refresh failing
        because the pipeline that feeds it failed; an SLA missed because the producer is down). The match is the
        dependency graph and the detection time, never a name.
        """
        items = st.upstream_ids | {st.incident.primary_item_id}
        open_incidents = await self._d.incidents.repository.list(
            IncidentQuery(statuses=[s for s in IncidentStatus if s.is_open], limit=1000)
        )
        blockers = sorted(
            (
                i
                for i in open_incidents
                if i.incident_id != st.incident.incident_id
                and i.primary_item_id in items
                and not is_sla_incident(i)
                and i.detected_at <= st.failure_time
            ),
            key=lambda i: i.detected_at,
        )
        for other in blockers:
            node = self._d.catalog.node_or_placeholder(other.primary_item_id)
            evidence = await self._derived(
                st,
                evidence_type=EvidenceType.HISTORICAL_INCIDENT,
                summary=f"Incident {other.incident_id} ({other.category}, {other.status.value}) is open on "
                f"{node.display_name}",
                source="orqentis.incident-store",
                resource_id=other.primary_item_id,
                resource_type=node.item_type.value,
                structured_payload={
                    "incident_id": other.incident_id,
                    "category": other.category,
                    "status": other.status.value,
                    "severity": other.severity.value,
                    "item_id": other.primary_item_id,
                    "detected_at": other.detected_at.isoformat(),
                    "title": other.title,
                },
                observed_at=other.detected_at,
                raw={"incident_id": other.incident_id, "category": other.category, "status": other.status.value},
            )
            st.a.upstream_incidents.append(
                UpstreamIncidentObservation(
                    evidence_id=evidence.evidence_id,
                    incident_id=other.incident_id,
                    item_id=other.primary_item_id,
                    item_name=node.display_name,
                    category=other.category,
                    status=other.status.value,
                    detected_at=other.detected_at,
                )
            )

    async def _record_errors(
        self,
        st: WorkflowState,
        sources: Sequence[tuple[str, ActivityRun | None]],
        *,
        run_source: str,
        run_resource_type: str,
        run_label: str = "Pipeline",
        run_error_code: str | None = None,
    ) -> None:
        """Classify each error message and record it as evidence; sets the errors, primary signature and failure points.

        ``sources`` is ``(message, activity)``: the activity that raised it, or ``None`` for an error of the run itself
        (then ``run_source``, ``run_resource_type``, ``run_label`` and ``run_error_code`` describe that run).
        """
        observations: list[ErrorObservation] = []
        for message, activity in sources:
            signature = self._d.classifier.classify(message)
            evidence = await self._derived(
                st,
                evidence_type=EvidenceType.ERROR_MESSAGE,
                summary=f"{activity.activity_name if activity else run_label} error: {message[:300]}",
                source=activity.source if activity else run_source,
                resource_id=(activity.target_item_id if activity else None) or st.incident.primary_item_id,
                resource_type=activity.activity_type if activity else run_resource_type,
                structured_payload={
                    "message": message,
                    "error_code": activity.error.error_code if activity and activity.error else run_error_code,
                    "activity_name": activity.activity_name if activity else None,
                    "activity_type": activity.activity_type if activity else None,
                    "target_item_id": activity.target_item_id if activity else None,
                    "signature_match": signature.model_dump() if signature else None,
                },
                observed_at=(activity.end_time if activity else None) or st.failure_time,
                raw=activity.model_dump(mode="json") if activity else {"message": message},
            )
            observations.append(
                ErrorObservation(
                    evidence_id=evidence.evidence_id,
                    message=evidence.structured_payload["message"],
                    error_code=evidence.structured_payload["error_code"],
                    activity_name=activity.activity_name if activity else None,
                    activity_type=activity.activity_type if activity else None,
                    target_item_id=activity.target_item_id if activity else None,
                    observed_at=evidence.observed_at,
                    signature=signature,
                )
            )
        st.a.errors = observations
        signatures = [o.signature for o in observations if o.signature]
        st.a.primary_signature = max(signatures, key=lambda s: s.priority) if signatures else None
        if not st.a.failure_point_ids:
            st.a.failure_point_ids = [st.incident.primary_item_id]

    async def _call(self, st: WorkflowState, name: str, **arguments: Any) -> ToolResponse:
        response = await self._d.registry.invoke(name, arguments, st.ctx)
        if response.status is not ToolStatus.SUCCESS:
            st.trace.errors.append(
                f"{name}: {response.status.value} " + "; ".join(f"{e.code}: {e.message}" for e in response.errors)
            )
        return response

    async def _evidence(
        self,
        st: WorkflowState,
        response: ToolResponse,
        evidence_type: EvidenceType,
        payload: dict[str, Any],
        *,
        summary: str | None = None,
        resource_id: str | None = None,
        resource_type: str | None = None,
        observed_at: datetime | None = None,
        source: str | None = None,
    ) -> Evidence:
        evidence = await st.recorder.record(
            evidence_type=evidence_type,
            summary=summary or str(response.metadata.get("summary") or response.tool_name),
            source=source or response.source,
            resource_id=resource_id,
            resource_type=resource_type,
            structured_payload=payload,
            observed_at=observed_at,
            tool_response=response,
        )
        self._d.incidents.add_timeline(
            st.incident,
            TimelineKind.EVIDENCE,
            f"{evidence.evidence_id}: {evidence.summary[:140]}",
            actor="workflow",
            evidence_ids=[evidence.evidence_id],
        )
        return evidence

    async def _derived(self, st: WorkflowState, **kwargs: Any) -> Evidence:
        """Record evidence derived by the workflow (not a single tool response) and note it on the timeline."""
        evidence = await st.recorder.record(**kwargs)
        self._d.incidents.add_timeline(
            st.incident,
            TimelineKind.EVIDENCE,
            f"{evidence.evidence_id}: {evidence.summary[:140]}",
            actor="workflow",
            evidence_ids=[evidence.evidence_id],
        )
        return evidence

    def _reset_analysis(self, incident: Incident) -> None:
        incident.findings = []
        incident.probable_root_causes = []
        incident.insufficient_evidence_reason = None
        incident.dependencies = []
        incident.affected_assets = []
        incident.blast_radius = None
        incident.sla_impact = []
        incident.related_incidents = []
        incident.matched_runbooks = []
        incident.recommended_actions = []
        incident.confidence = None
        incident.approval_state = ApprovalState.NOT_EVALUATED

    async def _resolve_metadata(self, st: WorkflowState) -> None:
        incident = st.incident
        for item_id in dict.fromkeys([incident.primary_item_id, *st.a.failure_point_ids]):
            response = await self._call(st, "get_item", item_id=item_id)
            if response.status is ToolStatus.SUCCESS:
                await self._evidence(
                    st,
                    response,
                    EvidenceType.ITEM_METADATA,
                    {"item": response.data},
                    resource_id=item_id,
                    resource_type=response.data.get("item_type"),
                )
        response = await self._call(st, "get_workspace", workspace_id=incident.workspace_id)
        if response.status is ToolStatus.SUCCESS:
            await self._evidence(
                st,
                response,
                EvidenceType.WORKSPACE_METADATA,
                response.data,
                resource_id=incident.workspace_id,
                resource_type="Workspace",
            )

    async def _upstream(self, st: WorkflowState) -> None:
        catalog = self._d.catalog
        for fp in st.a.failure_point_ids:
            response = await self._call(st, "get_upstream_dependencies", item_id=fp)
            if response.status is not ToolStatus.SUCCESS:
                continue
            nodes = response.data
            st.upstream_ids |= {n["item_id"] for n in nodes}
            evidence = await self._evidence(
                st,
                response,
                EvidenceType.DEPENDENCY_EDGE,
                {"direction": "upstream", "item_id": fp, "nodes": nodes},
                resource_id=fp,
            )
            st.a.upstream_evidence_ids.append(evidence.evidence_id)

        for source_id in sorted(st.upstream_ids):
            node = catalog.node(source_id)
            if node is None or node.item_type is not ItemType.EXTERNAL_SOURCE:
                continue
            response = await self._call(st, "get_item", item_id=source_id)
            if response.status is not ToolStatus.SUCCESS:
                continue
            connection = (response.data.get("metadata") or {}).get("connection") or {}
            expires = _iso(connection.get("secret_expires_at"))
            evidence = await self._evidence(
                st,
                response,
                EvidenceType.ITEM_METADATA,
                {"item": response.data, "connection": connection},
                resource_id=source_id,
                resource_type=ItemType.EXTERNAL_SOURCE.value,
                observed_at=expires,
                summary=f"Declared source {node.display_name}"
                + (f": connection {connection.get('id')} secret expires {expires.isoformat()}" if expires else ""),
            )
            if expires is not None:
                st.a.credentials.append(
                    CredentialObservation(
                        evidence_id=evidence.evidence_id,
                        item_id=source_id,
                        item_name=node.display_name,
                        connection_id=connection.get("id"),
                        client_app=connection.get("client_app"),
                        expires_at=expires,
                        expired_before_failure=expires <= st.failure_time,
                    )
                )

        watch = sorted(st.upstream_ids | set(st.a.failure_point_ids) | {st.incident.primary_item_id})
        response = await self._call(
            st,
            "get_operational_events",
            item_ids=watch[:25],
            event_types=[
                EventType.SCHEMA_CHANGED.value,
                EventType.DEPLOYMENT_COMPLETED.value,
                EventType.CREDENTIAL_EXPIRED.value,
            ],
            since=(st.failure_time - CAUSAL_WINDOW).isoformat(),
            until=st.failure_time.isoformat(),
        )
        if response.status is not ToolStatus.SUCCESS:
            return
        for raw in response.data:
            event = OperationalEvent.model_validate(raw)
            if event.event_type is not EventType.SCHEMA_CHANGED or not event.item_id:
                continue
            path = next(
                (p for fp in st.a.failure_point_ids if (p := self._d.graph.find_path(event.item_id, fp)) is not None),
                [],
            )
            evidence = await self._evidence(
                st,
                response,
                EvidenceType.SCHEMA_CHANGE,
                {**event.payload, "event_id": event.event_id, "upstream_path": path},
                summary=event.summary,
                resource_id=event.item_id,
                resource_type=event.item_type,
                observed_at=event.timestamp,
                source=event.source,
            )
            st.a.schema_changes.append(
                SchemaChangeObservation(
                    evidence_id=evidence.evidence_id,
                    item_id=event.item_id,
                    item_name=event.display_name or event.item_id,
                    table=event.payload.get("table"),
                    removed_columns=list(event.payload.get("removed_columns", [])),
                    added_columns=list(event.payload.get("added_columns", [])),
                    renamed_columns=[
                        (str(r["before"]), str(r["after"]))
                        for r in event.payload.get("renamed_columns", [])
                        if isinstance(r, dict) and "before" in r and "after" in r
                    ],
                    observed_at=event.timestamp,
                    upstream_path=path,
                )
            )

    async def _downstream(self, st: WorkflowState) -> None:
        d = self._d
        for fp in st.a.failure_point_ids:
            response = await self._call(st, "get_downstream_dependencies", item_id=fp)
            if response.status is not ToolStatus.SUCCESS:
                continue
            st.downstream_ids |= {n["item_id"] for n in response.data}
            evidence = await self._evidence(
                st,
                response,
                EvidenceType.DEPENDENCY_EDGE,
                {"direction": "downstream", "item_id": fp, "nodes": response.data},
                resource_id=fp,
            )
            st.a.downstream_evidence_ids.append(evidence.evidence_id)
        radius: BlastRadius = d.graph.get_blast_radius(st.a.failure_point_ids, also_impacted=st.skipped_ids)
        evidence = await self._derived(
            st,
            evidence_type=EvidenceType.BLAST_RADIUS,
            summary=radius.summary,
            source="orqentis.topology",
            resource_id=",".join(radius.origin_item_ids),
            structured_payload=radius.model_dump(mode="json"),
            raw=radius.model_dump(mode="json"),
        )
        st.a.blast_radius = radius
        st.a.blast_radius_evidence_id = evidence.evidence_id
        incident = st.incident
        incident.blast_radius = radius
        incident.affected_assets = [a for a in radius.assets if a.depth > 0]
        involved = st.upstream_ids | st.downstream_ids | set(st.a.failure_point_ids) | {incident.primary_item_id}
        incident.dependencies = [
            e for e in d.graph.edges if e.source_item_id in involved and e.target_item_id in involved
        ]

    async def _semantic_models(self, st: WorkflowState) -> None:
        radius = st.a.blast_radius
        if radius is None:
            return
        for asset in radius.assets:
            if asset.item_type is not ItemType.SEMANTIC_MODEL:
                continue
            response = await self._call(
                st, "get_semantic_model_refresh_history", semantic_model_id=asset.item_id, top=5
            )
            if response.status is ToolStatus.SUCCESS:
                evidence = await self._evidence(
                    st,
                    response,
                    EvidenceType.SEMANTIC_MODEL_REFRESH,
                    {"semantic_model_id": asset.item_id, "refreshes": response.data},
                    resource_id=asset.item_id,
                    resource_type=ItemType.SEMANTIC_MODEL.value,
                )
                st.a.refresh_evidence_ids.append(evidence.evidence_id)

    async def _sla(self, st: WorkflowState) -> None:
        response = await self._call(st, "get_sla", item_id=st.incident.primary_item_id)
        if response.status is not ToolStatus.SUCCESS:
            return
        assessments = [SLAAssessment.model_validate(a) for a in response.data]
        st.incident.sla_impact = assessments
        st.a.sla = assessments
        if assessments:
            evidence = await self._evidence(
                st,
                response,
                EvidenceType.SLA_STATUS,
                {"assessments": response.data},
                resource_id=st.incident.primary_item_id,
            )
            st.a.sla_evidence_id = evidence.evidence_id
        if any(a.state is SLAState.BREACHED for a in assessments) and st.incident.severity is not Severity.CRITICAL:
            await self._d.incidents.raise_severity(
                st.incident, Severity.CRITICAL, reason="SLA breached", actor="workflow"
            )

    async def _capacity(self, st: WorkflowState) -> None:
        response = await self._call(
            st,
            "get_capacity_context",
            workspace_id=st.incident.workspace_id,
            around=st.failure_time.isoformat(),
            window_minutes=60,
        )
        if response.status is not ToolStatus.SUCCESS:
            return
        stats = response.data.get("statistics", {})
        capacity = response.data.get("capacity") or {}
        throttle_start, throttle_end = _iso(stats.get("throttling_start")), _iso(stats.get("throttling_end"))
        observed = throttle_start or _iso(stats.get("peak_at"))
        evidence = await self._evidence(
            st,
            response,
            EvidenceType.CAPACITY_METRIC,
            {
                "capacity": capacity,
                "statistics": stats,
                "workspaces_on_capacity": response.data.get("workspaces_on_capacity"),
            },
            resource_id=capacity.get("id"),
            resource_type="Capacity",
            observed_at=observed,
        )
        during = bool(
            throttle_start and throttle_end and throttle_start <= st.failure_time <= throttle_end + SNAPSHOT_GRANULARITY
        )
        lineage = st.upstream_ids | st.downstream_ids | set(st.a.failure_point_ids) | {st.incident.primary_item_id}
        catalog = self._d.catalog
        contending = []
        for workload in stats.get("top_workloads", []):
            ws = catalog.workspaces.get(workload["workspace_id"])
            contending.append(
                ContendingWorkload(
                    item_id=workload["item_id"],
                    display_name=workload["display_name"],
                    workspace_id=workload["workspace_id"],
                    workspace_name=ws.display_name if ws else workload["workspace_id"],
                    environment=ws.environment if ws else Environment.UNKNOWN,
                    peak_cu_percent=workload["peak_cu_percent"],
                    cross_workspace=workload["workspace_id"] != st.incident.workspace_id,
                    in_incident_lineage=workload["item_id"] in lineage,
                )
            )
        st.a.capacity = CapacityObservation(
            evidence_id=evidence.evidence_id,
            capacity_id=capacity.get("id", "unknown"),
            capacity_name=capacity.get("display_name", capacity.get("id", "unknown")),
            sku=capacity.get("sku"),
            environment=Environment(capacity.get("environment", "UNKNOWN")),
            peak_cu_percent=stats.get("peak_cu_percent"),
            max_background_rejection_percent=stats.get("max_background_rejection_percent"),
            max_interactive_delay_percent=stats.get("max_interactive_delay_percent"),
            throttling_detected=bool(stats.get("throttling_detected")),
            failure_during_throttling=during,
            contending=contending,
        )

    async def _history(self, st: WorkflowState) -> None:
        incident = st.incident
        signature = st.a.primary_signature
        error_text = st.a.errors[0].message if st.a.errors else None
        response = await self._call(
            st,
            "get_recent_related_incidents",
            item_id=incident.primary_item_id,
            category=signature.category if signature else None,
            error_text=error_text,
        )
        if response.status is ToolStatus.SUCCESS:
            matches = [RelatedIncidentMatch.model_validate(m) for m in response.data]
            incident.related_incidents = matches
            st.a.related_incidents = matches
            if matches:
                evidence = await self._evidence(
                    st,
                    response,
                    EvidenceType.HISTORICAL_INCIDENT,
                    {"matches": response.data},
                    resource_id=incident.primary_item_id,
                )
                st.a.history_evidence_id = evidence.evidence_id
        if signature is None:
            return
        pattern = await self._d.history.recurring_pattern(incident, category=signature.category)
        if pattern is not None:
            evidence = await self._derived(
                st,
                evidence_type=EvidenceType.HISTORICAL_INCIDENT,
                summary=pattern.statement,
                source="orqentis.incident-history",
                resource_id=incident.primary_item_id,
                structured_payload=pattern.model_dump(mode="json"),
                raw=pattern.model_dump(mode="json"),
            )
            st.a.pattern = pattern
            st.a.pattern_evidence_id = evidence.evidence_id

    async def _deployments(self, st: WorkflowState) -> None:
        catalog = self._d.catalog
        for item_id in dict.fromkeys([*st.a.failure_point_ids, st.incident.primary_item_id]):
            node = catalog.node(item_id)
            if node is None or node.item_type not in _CODE_ITEMS:
                continue
            response = await self._call(st, "get_deployment_state", item_id=item_id)
            if response.status is not ToolStatus.SUCCESS or not response.data.get("comparison"):
                continue
            comparison = DeploymentComparison.model_validate(response.data["comparison"])
            evidence = await self._evidence(
                st,
                response,
                EvidenceType.DEPLOYMENT_COMPARISON,
                response.data["comparison"],
                resource_id=item_id,
                resource_type=node.item_type.value,
                observed_at=comparison.last_production_change,
            )
            st.a.deployments.append(
                DeploymentObservation(
                    evidence_id=evidence.evidence_id,
                    item_id=item_id,
                    item_name=node.display_name,
                    comparison=comparison,
                )
            )

    async def _runbooks(self, st: WorkflowState) -> None:
        signature = st.a.primary_signature
        text_parts = [e.message for e in st.a.errors] + [
            f"Schema change: column {c} removed" for s in st.a.schema_changes for c in s.removed_columns
        ]
        if st.a.capacity and st.a.capacity.failure_during_throttling:
            text_parts.append("capacity throttling: background rejection, exceeded its limits")
        item_types = {st.incident.primary_item_type or ItemType.DATA_PIPELINE}
        item_types |= {n.item_type for fp in st.a.failure_point_ids if (n := self._d.catalog.node(fp))}
        response = await self._call(
            st,
            "search_runbooks",
            category=signature.category if signature else None,
            text="\n".join(text_parts)[:8000],
            item_types=sorted(t.value for t in item_types),
        )
        if response.status is not ToolStatus.SUCCESS:
            return
        matches = [RunbookMatch.model_validate(m) for m in response.data]
        st.incident.matched_runbooks = matches
        st.a.runbooks = matches
        if matches:
            evidence = await self._evidence(st, response, EvidenceType.RUNBOOK_MATCH, {"matches": response.data})
            st.a.runbook_evidence_id = evidence.evidence_id

    def _package(self, st: WorkflowState) -> EvidencePackage:
        incident = st.incident
        return EvidencePackage(
            incident_id=incident.incident_id,
            title=incident.title,
            detected_category=incident.category,
            detected_at=incident.detected_at,
            primary_item=PrimaryItemView(
                item_id=incident.primary_item_id,
                display_name=incident.primary_item_name or incident.primary_item_id,
                item_type=incident.primary_item_type,
                workspace_id=incident.workspace_id,
                environment=incident.environment,
            ),
            evidence=incident.evidence[st.first_evidence_index :],
            context=st.a,
        )

    def _confidence(
        self, st: WorkflowState, root: Finding | None, category: str | None, runbook_id: str | None
    ) -> None:
        incident = st.incident
        signals = ConfidenceSignals()
        if root is not None and category is not None:
            signals = self._signals(st, root, category, runbook_id)
        assessment = self._d.confidence.assess(signals)
        incident.confidence = assessment
        signature = st.a.primary_signature
        incident.category = category or IncidentCategory.UNKNOWN.value
        if root is None:
            self._d.incidents.add_timeline(
                incident,
                TimelineKind.AGENT,
                f"Root cause undetermined; confidence {assessment.level.value}",
                actor=AGENT_PRINCIPAL.id,
            )
            if signature is not None:
                incident.metadata["unconfirmed_signature"] = signature.signature_id
            return
        root.confidence = assessment.score
        incident.probable_root_causes = [
            RootCauseCandidate(
                rank=1,
                statement=root.statement,
                category=category,
                finding_id=root.finding_id,
                evidence_ids=root.evidence_ids,
                confidence=assessment,
            )
        ]
        self._d.incidents.add_timeline(
            incident,
            TimelineKind.AGENT,
            f"Probable root cause {category} ({assessment.level.value} {assessment.score:.2f})",
            actor=AGENT_PRINCIPAL.id,
            evidence_ids=root.evidence_ids,
        )

    def _signature_signal(self, st: WorkflowState, category: str) -> Signal:
        """How well the error text identifies the category (the SLA workflow, which has no error, overrides this)."""
        signature: SignatureMatch | None = st.a.primary_signature
        primary_error_ids = sorted(e.evidence_id for e in st.a.errors)
        if signature is not None and signature.category == category:
            return Signal(
                1.0, f"Error matches signature {signature.signature_id} ({signature.category})", primary_error_ids
            )
        if signature is not None:
            return Signal(0.5, f"Error matches {signature.signature_id} but it maps to {signature.category}")
        return Signal(0.0, "Error matches no known signature")

    def _signals(self, st: WorkflowState, root: Finding, category: str, runbook_id: str | None) -> ConfidenceSignals:
        evidence = st.incident.evidence_by_id()
        cited = [evidence[i] for i in root.evidence_ids if i in evidence]
        primary_error_ids = {e.evidence_id for e in st.a.errors}
        sig = self._signature_signal(st, category)

        match = next((m for m in st.a.runbooks if m.runbook_id == runbook_id and m.category == category), None)
        runbook = (
            Signal(
                match.score,
                f"{match.runbook_id} v{match.version} matched with score {match.score:.2f}",
                [st.a.runbook_evidence_id] if st.a.runbook_evidence_id else [],
            )
            if match
            else None
        )

        similar = [m for m in st.a.related_incidents if m.category == category]
        history = (
            Signal(
                max(m.similarity for m in similar),
                f"{len(similar)} similar incident(s): {', '.join(m.incident_id for m in similar)}",
                [st.a.history_evidence_id] if st.a.history_evidence_id else [],
            )
            if similar
            else None
        )

        upstream_hits = [e for e in cited if e.resource_id in st.upstream_ids]
        capacity_id = st.a.capacity.capacity_id if st.a.capacity else None
        shared_hits = [e for e in cited if capacity_id and e.resource_id == capacity_id]
        if upstream_hits:
            dependency = Signal(
                1.0,
                "Root-cause evidence is on items upstream of the failure point: "
                + ", ".join(sorted({e.resource_id or "" for e in upstream_hits})),
                [e.evidence_id for e in upstream_hits],
            )
        elif shared_hits:
            dependency = Signal(
                0.5,
                f"Root-cause evidence is on the shared capacity {capacity_id}",
                [e.evidence_id for e in shared_hits],
            )
        else:
            dependency = None

        causal = [
            e
            for e in cited
            if e.evidence_id not in primary_error_ids
            and e.observed_at is not None
            and st.failure_time - CAUSAL_WINDOW <= e.observed_at <= st.failure_time
        ]
        temporal = (
            Signal(
                1.0,
                "Causal evidence precedes the failure within 24h: "
                + ", ".join(
                    f"{e.evidence_id.rsplit('-', 1)[-1]} at {e.observed_at.isoformat()}"
                    for e in causal
                    if e.observed_at
                ),
                [e.evidence_id for e in causal],
            )
            if causal
            else None
        )

        sources = sorted({e.source for e in cited})
        corroboration = Signal(
            corroboration_value(len(sources)),
            f"{len(sources)} independent source(s): {', '.join(sources)}",
            [e.evidence_id for e in cited],
        )
        return ConfidenceSignals(
            error_signature_match=sig,
            runbook_match=runbook,
            historical_incident_match=history,
            dependency_correlation=dependency,
            temporal_correlation=temporal,
            corroborating_sources=corroboration,
        )

    def _targets(self, st: WorkflowState, target: ActionTarget) -> list[str]:
        catalog = self._d.catalog
        if target is ActionTarget.PRIMARY_ITEM:
            return [st.incident.primary_item_id]
        if target is ActionTarget.FAILURE_POINT:
            return st.a.failure_point_ids[:1]
        if target is ActionTarget.AFFECTED_SEMANTIC_MODELS:
            return [a.item_id for a in st.incident.affected_assets if a.item_type is ItemType.SEMANTIC_MODEL]
        if target is ActionTarget.UPSTREAM_SOURCE:
            return [
                i
                for i in sorted(st.upstream_ids)
                if (n := catalog.node(i)) is not None and n.item_type is ItemType.EXTERNAL_SOURCE
            ][:1]
        if target is ActionTarget.CONTENDING_WORKLOAD and st.a.capacity:
            return [w.item_id for w in st.a.capacity.contending if not w.in_incident_lineage][:1]
        if target is ActionTarget.CAPACITY and st.a.capacity:
            return [st.a.capacity.capacity_id]
        return []

    def _action(
        self,
        st: WorkflowState,
        *,
        action_type: ActionType,
        title: str,
        target_id: str,
        runbook: Runbook | None,
        parameters: dict[str, Any],
        idempotent: bool,
        description: str = "",
    ) -> Action:
        catalog = self._d.catalog
        node = catalog.node(target_id)
        capacity = catalog.capacities.get(target_id)
        return Action(
            action_id=new_id("ACT"),
            incident_id=st.incident.incident_id,
            action_type=action_type,
            title=title,
            description=description,
            target_item_id=target_id,
            target_display_name=node.display_name if node else (capacity.display_name if capacity else target_id),
            workspace_id=node.workspace_id if node else None,
            environment=node.environment if node else (capacity.environment if capacity else Environment.UNKNOWN),
            parameters=parameters,
            idempotent=idempotent,
            requested_by=AGENT_PRINCIPAL.id,
            runbook_id=runbook.id if runbook else None,
            created_at=self._d.clock.now(),
        )

    def _recommendation(self, st: WorkflowState, runbook_id: str | None, root: Finding | None) -> Recommendation:
        incident = st.incident
        runbook = self._d.runbooks.get(runbook_id) if runbook_id else None
        evidence_ids = [i for i in [st.a.runbook_evidence_id, *(root.evidence_ids if root else [])] if i]
        if runbook is None:
            owner = self._d.catalog.node_or_placeholder(incident.primary_item_id).owner or "the item owner"
            return Recommendation(
                recommendation_id=new_id("REC"),
                incident_id=incident.incident_id,
                title="Escalate for manual investigation",
                summary="No runbook applies and the root cause is undetermined. Escalate with the evidence package; "
                "FabricOps proposes no automated change.",
                steps=[
                    f"Escalate to {owner} with this report and the evidence ids.",
                    "Collect detailed activity logs for the failed run from the Fabric Monitoring Hub.",
                    "Re-run the investigation when new evidence is available.",
                ],
                actions=[
                    self._action(
                        st,
                        action_type=ActionType.INSPECT_LOGS,
                        target_id=incident.primary_item_id,
                        title="Inspect detailed run logs in the Monitoring Hub",
                        runbook=None,
                        parameters={"run_id": incident.correlation_id},
                        idempotent=True,
                    )
                ],
                risk=RiskLevel.READ_ONLY,
                approval_required=False,
                validation_plan=["Root cause identified and confirmed by the item owner."],
                escalation=[f"Owner: {owner}", "Platform on-call (Fabric platform team)"],
                evidence_ids=evidence_ids,
                produced_by="orqentis.workflow",
                created_at=self._d.clock.now(),
            )
        actions: list[Action] = []
        for template in runbook.remediation_actions:
            for target_id in self._targets(st, template.target):
                actions.append(
                    self._action(
                        st,
                        action_type=template.action_type,
                        title=template.title,
                        target_id=target_id,
                        runbook=runbook,
                        parameters=template.parameters,
                        idempotent=template.idempotent,
                        description=f"From {runbook.id} v{runbook.version}",
                    )
                )
        return Recommendation(
            recommendation_id=new_id("REC"),
            incident_id=incident.incident_id,
            title=f"Apply {runbook.id}: {runbook.title}",
            summary=f"Follow {runbook.id} v{runbook.version} ({runbook.title}). "
            + "; ".join(f"{a.title} [{a.target_display_name}]" for a in actions)
            + ".",
            runbook_id=runbook.id,
            runbook_version=runbook.version,
            steps=runbook.section_steps("Recommended Remediation"),
            actions=actions,
            risk=runbook.risk,
            approval_required=runbook.approval_required,
            validation_plan=runbook.section_steps("Validation"),
            rollback=runbook.section_steps("Rollback"),
            escalation=runbook.section_steps("Escalation"),
            evidence_ids=evidence_ids,
            produced_by=f"runbook:{runbook.id}",
            created_at=self._d.clock.now(),
        )

    async def _policy(self, st: WorkflowState, recommendation: Recommendation) -> None:
        incident = st.incident
        effects: list[PolicyEffect] = []
        for action in recommendation.actions:
            if self._d.auto_request_approval:
                outcome = await self._d.gateway.submit(action, principal=AGENT_PRINCIPAL, incident=incident)
                decision = outcome.decision
                if (
                    outcome.status is GatewayStatus.ACTION_REQUIRES_APPROVAL
                    and outcome.approval is not None
                    and outcome.approval.approval_id not in incident.approval_ids
                ):
                    incident.approval_ids.append(outcome.approval.approval_id)
                    st.trace.approval_ids.append(outcome.approval.approval_id)
            else:
                decision = self._d.policy.evaluate(action)
                action.risk = decision.risk
                action.policy_decision = decision
            st.trace.policy_decisions.append(decision)
            effects.append(decision.effect)
        risks = [a.risk for a in recommendation.actions if a.risk is not None]
        recommendation.risk = RiskLevel.highest(risks) if risks else recommendation.risk
        recommendation.approval_required = PolicyEffect.REQUIRE_APPROVAL in effects
        if self._d.auto_request_approval and recommendation.approval_required:
            incident.approval_state = ApprovalState.PENDING
        elif recommendation.approval_required:
            incident.approval_state = ApprovalState.NOT_EVALUATED
        else:
            incident.approval_state = ApprovalState.NOT_REQUIRED
