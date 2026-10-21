"""Data product SLA breach investigation (ADR-0020).

Opened by the SLA breach rule from the sweep's ``SLA_ASSESSED`` event. The question is not *what error occurred*,
because often none did, but *why the item that has to refresh the product did not finish by the deadline*:

* **LATE**: it did refresh, after the deadline. The run's start time and duration are compared with its earlier runs.
* **BLOCKED**: an incident on the item, or on something upstream of it, was open before the deadline.
* **FAILED**: its last run failed and nothing is open for it.
* **NOT_RUN**: nothing finished in the SLA window.

The root-cause category is always ``SLA_BREACH``; the statement carries the why. A breach with a blocking incident
is still its own incident (the business impact is tracked separately), but it names the blocker.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from orqentis.agents.models import SlaBreachObservation
from orqentis.domain.common import ItemType
from orqentis.domain.events import EventType, OperationalEvent
from orqentis.domain.evidence import Evidence, EvidenceType
from orqentis.domain.incidents import Incident
from orqentis.domain.runs import PipelineRun
from orqentis.domain.sla import SLA, SLAAssessment
from orqentis.incidents.confidence import Signal
from orqentis.incidents.run_history import RunPoint, points_from_events, points_from_runs, summarize, usual_start_local
from orqentis.incidents.sla import is_sla_incident
from orqentis.incidents.workflows.common import DiagnosticWorkflow, WorkflowState
from orqentis.incidents.workflows.item_job_failure import JOB_ITEM_TYPES
from orqentis.tools.models import ToolStatus

_RUNS = 15
_JOB_TYPES = JOB_ITEM_TYPES | {ItemType.DATA_PIPELINE}
_LABELS = {
    ItemType.DATA_PIPELINE: "Pipeline",
    ItemType.NOTEBOOK: "Notebook",
    ItemType.SPARK_JOB_DEFINITION: "Spark job definition",
    ItemType.DATAFLOW: "Dataflow",
    ItemType.SEMANTIC_MODEL: "Refresh",
}


def sla_id_of(incident: Incident) -> str | None:
    """``sla:<sla id>:<deadline date>`` is the correlation id the sweep gives its events."""
    correlation = incident.correlation_id or ""
    if not correlation.startswith("sla:") or correlation.count(":") < 2:
        return None
    return correlation.removeprefix("sla:").rsplit(":", 1)[0] or None


class SlaBreachWorkflow(DiagnosticWorkflow):
    name = "sla-breach-v1"
    noun = "freshness item"

    def supports(self, incident: Incident) -> bool:
        return is_sla_incident(incident)

    async def _observe(self, st: WorkflowState) -> None:
        trace, clock = st.trace, self._d.clock
        with trace.step(2, "Fetch SLA assessment", clock):
            await self._fetch_assessment(st)
        if st.analysis is None:  # the assessment could not be read: the analysis opens at the detection time
            st.start_analysis()
        with trace.step(3, "Fetch freshness item runs", clock):
            await self._fetch_runs(st)
        with trace.step(4, "Capture error evidence", clock):
            await self._capture_errors(st)

    async def _after_upstream(self, st: WorkflowState) -> None:
        await self._record_upstream_incidents(st)
        self._classify(st)

    def _dependency_evidence(self, st: WorkflowState, cited: list[Evidence]) -> list[Evidence]:
        """An incident on the freshness item, or upstream of it, that was open when the deadline passed."""
        blockers = {b.evidence_id for b in st.a.upstream_incidents}
        return [e for e in cited if e.evidence_id in blockers]

    def _runbook_category(self, st: WorkflowState) -> str | None:
        return "SLA_BREACH"

    def _runbook_text(self, st: WorkflowState) -> list[str]:
        breach = st.a.sla_breach
        return [f"SLA breach: {breach.kind.lower()}"] if breach else []

    async def _sla(self, st: WorkflowState) -> None:
        """Already done in step 2: the assessment is what this incident is about, and the rule set the severity."""
        return None

    def _signature_signal(self, st: WorkflowState, category: str) -> Signal:
        breach = st.a.sla_breach
        if breach is None:
            return Signal(0.0, "No SLA assessment could be read")
        return Signal(
            1.0 if category == "SLA_BREACH" else 0.5,
            "The deterministic SLA evaluation (from freshness events) reports the SLA breached",
            [breach.evidence_id],
        )

    # ------------------------------------------------------------------------------------------ steps
    def _sla_of(self, st: WorkflowState) -> SLA | None:
        sla_id = sla_id_of(st.incident)
        return self._d.catalog.slas.get(sla_id) if sla_id else None

    async def _fetch_assessment(self, st: WorkflowState) -> None:
        incident = st.incident
        sla_id = sla_id_of(incident)
        response = await self._call(st, "get_sla", item_id=incident.primary_item_id)
        if response.status is not ToolStatus.SUCCESS:
            return
        assessments = [SLAAssessment.model_validate(a) for a in response.data]
        mine = next((a for a in assessments if a.sla_id == sla_id), None)
        if mine is not None and mine.deadline is not None:
            st.failure_time = mine.deadline  # the moment the SLA was missed
        st.start_analysis()
        incident.sla_impact = assessments
        st.a.sla = assessments
        evidence = await self._evidence(
            st,
            response,
            EvidenceType.SLA_STATUS,
            {"assessments": response.data, "investigated_sla_id": sla_id},
            resource_id=incident.primary_item_id,
            # An evaluation, not an event that preceded the breach: no observed_at, so it never counts as causal.
        )
        st.a.sla_evidence_id = evidence.evidence_id
        if mine is None:
            st.trace.errors.append("the SLA that opened the incident is not among the SLAs downstream of its item")
            return
        st.scratch["assessment"] = mine

    async def _fetch_runs(self, st: WorkflowState) -> None:
        incident = st.incident
        item_type = incident.primary_item_type
        points: list[RunPoint] = []
        if item_type in _JOB_TYPES:
            response = await self._call(st, "get_job_instances", item_id=incident.primary_item_id, limit=_RUNS)
            if response.status is not ToolStatus.SUCCESS:
                return
            runs = [PipelineRun.model_validate(r) for r in response.data]
            points = points_from_runs(runs)
        elif item_type is ItemType.SEMANTIC_MODEL:
            response = await self._call(
                st,
                "get_operational_events",
                item_ids=[incident.primary_item_id],
                event_types=[EventType.REFRESH_FAILED.value, EventType.REFRESH_SUCCEEDED.value],
                since=(st.failure_time - timedelta(days=14)).isoformat(),
                limit=100,
                newest_first=True,  # the limit must cut the oldest runs, not the latest ones
            )
            if response.status is not ToolStatus.SUCCESS:
                return
            points = points_from_events(OperationalEvent.model_validate(e) for e in response.data)
        else:
            st.trace.errors.append(f"no run history is collected for {item_type.value if item_type else 'this item'}")
            return
        deadline = st.failure_time
        window = [p for p in points if p.when is not None and p.when > deadline - timedelta(days=1)]
        late = next((p for p in window if p.ok and p.when is not None and p.when > deadline), None)
        failed = next((p for p in window if p.ok is False), None)
        before = [p for p in points if p is not late]
        summary = summarize(before)
        sla = self._sla_of(st)
        evidence = await self._evidence(
            st,
            response,
            EvidenceType.RUN_HISTORY,
            {
                "runs": [
                    {"run_id": p.run_id, "ok": p.ok, "started_at": p.started_at, "ended_at": p.ended_at} for p in points
                ],
                "window_start": deadline - timedelta(days=1),
                "deadline": deadline,
            },
            summary=f"{len(points)} recent run(s) of the freshness item; "
            + (
                f"one finished after the deadline at {late.when.isoformat()}"
                if late and late.when
                else "none finished after the deadline"
            ),
            resource_id=incident.primary_item_id,
            resource_type=item_type.value if item_type else None,
            observed_at=late.when if late else (summary.last_success_at if summary.last_success_at else None),
        )
        st.scratch.update(
            late=late,
            failed=failed,
            summary=summary,
            run_evidence_id=evidence.evidence_id,
            usual_start=usual_start_local(before, sla.timezone) if sla else None,
        )

    async def _capture_errors(self, st: WorkflowState) -> None:
        failed: RunPoint | None = st.scratch.get("failed")
        item_type = st.incident.primary_item_type
        sources = [(failed.error, None)] if failed is not None and failed.error else []
        await self._record_errors(
            st,
            sources,
            run_source="fabric.jobs",
            run_resource_type=item_type.value if item_type else "Item",
            run_label=_LABELS.get(item_type, "Run") if item_type else "Run",
        )

    # ------------------------------------------------------------------------------------------ classification
    def _classify(self, st: WorkflowState) -> None:
        assessment: SLAAssessment | None = st.scratch.get("assessment")
        if assessment is None or assessment.deadline is None or st.a.sla_evidence_id is None:
            return
        scratch: dict[str, Any] = st.scratch
        late: RunPoint | None = scratch.get("late")
        failed: RunPoint | None = scratch.get("failed")
        blockers = st.a.upstream_incidents
        summary = scratch.get("summary")
        kind: Any
        if late is not None:
            kind = "LATE"
        elif blockers:
            kind = "BLOCKED"
        elif failed is not None:
            kind = "FAILED"
        else:
            kind = "NOT_RUN"
        done = late.when if late is not None else assessment.last_successful_update
        st.a.sla_breach = SlaBreachObservation(
            evidence_id=st.a.sla_evidence_id,
            sla_id=assessment.sla_id,
            data_product_name=assessment.data_product_name,
            deadline=assessment.deadline,
            kind=kind,
            last_successful_update=done if kind == "LATE" else assessment.last_successful_update,
            minutes_late=((done - assessment.deadline).total_seconds() / 60 if kind == "LATE" and done else None),
            run_evidence_id=scratch.get("run_evidence_id"),
            started_at=late.started_at if late is not None else None,
            usual_start_local=scratch.get("usual_start"),
            duration_seconds=late.duration_seconds if late is not None else None,
            median_duration_seconds=summary.median_duration_seconds if summary is not None else None,
            blocking_incident_ids=[b.incident_id for b in blockers],
        )
