"""Pipeline failure investigation: deterministic orchestration around the FabricOps Investigator.

The numbered steps mirror the product specification. Every tool call goes through the tool
registry (logged, audited, traced) and every fact used later is recorded as evidence. The steps every
investigation shares live in :class:`~orqentis.incidents.workflows.common.DiagnosticWorkflow`; this class supplies
what is specific to a pipeline: its run, its activity runs and the errors they carry (steps 2 to 4).
"""

from __future__ import annotations

from orqentis.domain.common import ItemType
from orqentis.domain.evidence import EvidenceType
from orqentis.domain.incidents import Incident
from orqentis.domain.runs import ActivityRun, PipelineRun
from orqentis.incidents.workflows.common import (
    SKIPPED_STATUSES,
    DiagnosticWorkflow,
    WorkflowDependencies,
    WorkflowState,
    is_sla_incident,
)
from orqentis.tools.models import ToolStatus

__all__ = ["PipelineFailureWorkflow", "WorkflowDependencies"]


class PipelineFailureWorkflow(DiagnosticWorkflow):
    name = "pipeline-failure-v1"
    noun = "pipeline"

    def supports(self, incident: Incident) -> bool:
        return incident.primary_item_type is ItemType.DATA_PIPELINE and not is_sla_incident(incident)

    async def _observe(self, st: WorkflowState) -> None:
        trace, clock = st.trace, self._d.clock
        with trace.step(2, "Fetch pipeline run", clock):
            await self._fetch_run(st)
        st.start_analysis()
        with trace.step(3, "Fetch activity failures", clock):
            await self._fetch_activities(st)
        with trace.step(4, "Capture error evidence", clock):
            await self._capture_errors(st)

    # ------------------------------------------------------------------------------------------ steps
    async def _fetch_run(self, st: WorkflowState) -> None:
        incident = st.incident
        if not incident.correlation_id:
            st.trace.errors.append("incident has no run correlation id")
            return
        response = await self._call(
            st, "get_pipeline_run", pipeline_id=incident.primary_item_id, run_id=incident.correlation_id
        )
        if response.status is not ToolStatus.SUCCESS:
            return
        run = PipelineRun.model_validate(response.data)
        st.run = run
        st.failure_time = run.end_time or run.start_time or incident.detected_at
        await self._evidence(
            st,
            response,
            EvidenceType.PIPELINE_FAILURE,
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
            resource_type=ItemType.DATA_PIPELINE.value,
            observed_at=st.failure_time,
        )

    async def _fetch_activities(self, st: WorkflowState) -> None:
        if st.run is None:
            return
        response = await self._call(
            st, "get_pipeline_activity_runs", pipeline_id=st.incident.primary_item_id, run_id=st.run.id
        )
        if response.status is not ToolStatus.SUCCESS:
            return
        st.activities = [ActivityRun.model_validate(a) for a in response.data]
        await self._evidence(
            st,
            response,
            EvidenceType.ACTIVITY_FAILURE,
            {
                "activities": [
                    {
                        "activity_name": a.activity_name,
                        "activity_type": a.activity_type,
                        "status": a.status.value,
                        "target_item_id": a.target_item_id,
                        "error": a.error.message if a.error else None,
                    }
                    for a in st.activities
                ]
            },
            resource_id=st.incident.primary_item_id,
            resource_type=ItemType.DATA_PIPELINE.value,
            observed_at=st.failure_time,
        )
        catalog = self._d.catalog
        failed = [a for a in st.activities if a.status.is_failure and a.target_item_id in catalog.items]
        st.a.failure_point_ids = list(dict.fromkeys(a.target_item_id for a in failed if a.target_item_id))
        st.skipped_ids = [
            a.target_item_id
            for a in st.activities
            if a.status in SKIPPED_STATUSES and a.target_item_id and catalog.node(a.target_item_id)
        ]

    async def _capture_errors(self, st: WorkflowState) -> None:
        sources: list[tuple[str, ActivityRun | None]] = [
            (a.error.message, a) for a in st.activities if a.status.is_failure and a.error
        ]
        if not sources and st.run is not None and st.run.failure is not None:
            sources = [(st.run.failure.message, None)]
        await self._record_errors(st, sources, run_source="fabric.jobs", run_resource_type=ItemType.DATA_PIPELINE.value)
