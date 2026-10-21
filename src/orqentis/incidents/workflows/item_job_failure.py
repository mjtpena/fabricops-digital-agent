"""Notebook, Spark job definition and Dataflow failure investigation (ADR-0020).

Opened by the job rules for a run that failed on its own (a person or a schedule started it). A job that a pipeline
started is the pipeline's incident and is investigated by :class:`PipelineFailureWorkflow`. The steps after the
failure itself (lineage, blast radius, SLA, capacity, history, runbooks, confidence, recommendation, policy, report) are
the shared ones of :class:`~orqentis.incidents.workflows.common.DiagnosticWorkflow`.
"""

from __future__ import annotations

from orqentis.agents.models import RunHistoryObservation
from orqentis.domain.common import ItemType
from orqentis.domain.evidence import EvidenceType
from orqentis.domain.incidents import Incident
from orqentis.domain.runs import PipelineRun
from orqentis.incidents.run_history import points_from_runs, summarize
from orqentis.incidents.workflows.common import DiagnosticWorkflow, WorkflowState, is_sla_incident
from orqentis.tools.models import ToolStatus

JOB_ITEM_TYPES = frozenset({ItemType.NOTEBOOK, ItemType.SPARK_JOB_DEFINITION, ItemType.DATAFLOW})
_LABELS = {
    ItemType.NOTEBOOK: "Notebook",
    ItemType.SPARK_JOB_DEFINITION: "Spark job definition",
    ItemType.DATAFLOW: "Dataflow",
}
_HISTORY_RUNS = 10


class ItemJobFailureWorkflow(DiagnosticWorkflow):
    name = "item-job-failure-v1"
    noun = "job"

    def supports(self, incident: Incident) -> bool:
        return incident.primary_item_type in JOB_ITEM_TYPES and not is_sla_incident(incident)

    async def _observe(self, st: WorkflowState) -> None:
        trace, clock = st.trace, self._d.clock
        with trace.step(2, "Fetch job instance", clock):
            await self._fetch_job(st)
        st.start_analysis()
        with trace.step(3, "Fetch recent runs", clock):
            await self._fetch_history(st)
        with trace.step(4, "Capture error evidence", clock):
            await self._capture_errors(st)

    # ------------------------------------------------------------------------------------------ steps
    async def _fetch_job(self, st: WorkflowState) -> None:
        incident = st.incident
        if not incident.correlation_id:
            st.trace.errors.append("incident has no run correlation id")
            return
        response = await self._call(
            st, "get_job_instance", item_id=incident.primary_item_id, run_id=incident.correlation_id
        )
        if response.status is not ToolStatus.SUCCESS:
            return
        run = PipelineRun.model_validate(response.data)
        st.run = run
        st.failure_time = run.end_time or run.start_time or incident.detected_at
        await self._evidence(
            st,
            response,
            EvidenceType.JOB_FAILURE,
            {
                "run_id": run.id,
                "status": run.status.value,
                "invoke_type": run.invoke_type,
                "start_time": response.data.get("start_time"),
                "end_time": response.data.get("end_time"),
                "duration_seconds": run.duration_seconds,
                "error_code": run.failure.error_code if run.failure else None,
                "error_message": run.failure.message if run.failure else None,
            },
            resource_id=run.item_id,
            resource_type=incident.primary_item_type.value if incident.primary_item_type else None,
            observed_at=st.failure_time,
        )

    async def _fetch_history(self, st: WorkflowState) -> None:
        incident = st.incident
        response = await self._call(st, "get_job_instances", item_id=incident.primary_item_id, limit=_HISTORY_RUNS)
        if response.status is not ToolStatus.SUCCESS:
            return
        runs = [PipelineRun.model_validate(r) for r in response.data]
        # The run being investigated is part of the history; a streak counts it.
        summary = summarize(points_from_runs(runs))
        evidence = await self._evidence(
            st,
            response,
            EvidenceType.RUN_HISTORY,
            {
                "runs": [
                    {"run_id": r.id, "status": r.status.value, "start_time": r.start_time, "end_time": r.end_time}
                    for r in runs
                ],
                "last_success_at": summary.last_success_at,
                "consecutive_failures": summary.consecutive_failures,
            },
            resource_id=incident.primary_item_id,
            resource_type=incident.primary_item_type.value if incident.primary_item_type else None,
            observed_at=summary.last_success_at,
        )
        st.a.run_history = RunHistoryObservation(
            evidence_id=evidence.evidence_id,
            item_id=incident.primary_item_id,
            runs_considered=summary.runs_considered,
            last_success_at=summary.last_success_at,
            consecutive_failures=summary.consecutive_failures,
            first_failure_at=summary.first_failure_at,
            median_duration_seconds=summary.median_duration_seconds,
        )

    async def _capture_errors(self, st: WorkflowState) -> None:
        run = st.run
        sources = [(run.failure.message, None)] if run is not None and run.failure is not None else []
        item_type = st.incident.primary_item_type
        await self._record_errors(
            st,
            sources,
            run_source=run.source if run else "fabric.jobs",
            run_resource_type=item_type.value if item_type else "Item",
            run_label=_LABELS.get(item_type, "Job") if item_type else "Job",
            run_error_code=run.failure.error_code if run and run.failure else None,
        )
