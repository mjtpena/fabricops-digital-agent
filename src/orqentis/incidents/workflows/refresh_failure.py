"""Semantic model refresh failure investigation (decision D9, ADR-0020).

A refresh failure reaches an investigation by two routes, and both end in the same category and runbook:

* **The model route** (this workflow): the refresh outcome came from Fabric workspace monitoring (or, where the
  deployment can read it, the Power BI history) and the rule opened an incident on the semantic model. The evidence is
  the stored refresh event, which keeps the source's own provenance.
* **The pipeline route**: a pipeline's refresh activity failed, so the incident is the pipeline's. The shared error
  classification recognises the engine's refresh error (``SIG-MODEL-REFRESH``), and the runbook search includes the
  semantic model among the failure points, so :class:`PipelineFailureWorkflow` reaches the same root-cause category
  and runbook. When both routes fire for one failure, the model's incident is not opened (see the detection engine).

The model route also looks upstream: a refresh that fails while the pipeline feeding it has an open incident is most
likely a consequence of it.
"""

from __future__ import annotations

from datetime import timedelta

from orqentis.agents.models import RunHistoryObservation
from orqentis.domain.common import ItemType
from orqentis.domain.events import EventType, OperationalEvent
from orqentis.domain.evidence import EvidenceType
from orqentis.domain.incidents import Incident
from orqentis.incidents.run_history import points_from_events, summarize
from orqentis.incidents.sla import is_sla_incident
from orqentis.incidents.workflows.common import DiagnosticWorkflow, WorkflowState
from orqentis.tools.models import ToolStatus

_HISTORY_WINDOW = timedelta(days=14)
_TRIGGER_WINDOW = timedelta(hours=1)


class RefreshFailureWorkflow(DiagnosticWorkflow):
    name = "refresh-failure-v1"
    noun = "semantic model"

    def supports(self, incident: Incident) -> bool:
        return incident.primary_item_type is ItemType.SEMANTIC_MODEL and not is_sla_incident(incident)

    async def _observe(self, st: WorkflowState) -> None:
        trace, clock = st.trace, self._d.clock
        with trace.step(2, "Fetch refresh failure", clock):
            await self._fetch_refresh(st)
        st.start_analysis()
        with trace.step(3, "Fetch refresh history", clock):
            await self._fetch_history(st)
        with trace.step(4, "Capture error evidence", clock):
            await self._capture_errors(st)

    async def _after_upstream(self, st: WorkflowState) -> None:
        await self._record_upstream_incidents(st)

    # ------------------------------------------------------------------------------------------ steps
    async def _fetch_refresh(self, st: WorkflowState) -> None:
        incident = st.incident
        response = await self._call(
            st,
            "get_operational_events",
            item_ids=[incident.primary_item_id],
            event_types=[EventType.REFRESH_FAILED.value],
            since=(incident.detected_at - _TRIGGER_WINDOW).isoformat(),
            until=(incident.detected_at + _TRIGGER_WINDOW).isoformat(),
        )
        if response.status is not ToolStatus.SUCCESS:
            return
        events = [OperationalEvent.model_validate(e) for e in response.data]
        trigger_id = incident.metadata.get("trigger_event_id")
        event = next(
            (e for e in events if incident.correlation_id and e.correlation_id == incident.correlation_id),
            next((e for e in events if e.event_id == trigger_id), None),
        )
        if event is None:
            st.trace.errors.append("the refresh failure that opened the incident is not in the event store")
            return
        st.scratch["refresh_event"] = event
        st.failure_time = event.timestamp
        await self._evidence(
            st,
            response,
            EvidenceType.SEMANTIC_MODEL_REFRESH,
            {**event.payload, "event_id": event.event_id},
            summary=event.summary,
            resource_id=event.item_id,
            resource_type=ItemType.SEMANTIC_MODEL.value,
            observed_at=event.timestamp,
            source=event.source,
        )

    async def _fetch_history(self, st: WorkflowState) -> None:
        incident = st.incident
        response = await self._call(
            st,
            "get_operational_events",
            item_ids=[incident.primary_item_id],
            event_types=[EventType.REFRESH_FAILED.value, EventType.REFRESH_SUCCEEDED.value],
            since=(st.failure_time - _HISTORY_WINDOW).isoformat(),
            until=st.failure_time.isoformat(),
            limit=100,
        )
        if response.status is not ToolStatus.SUCCESS:
            return
        events = [OperationalEvent.model_validate(e) for e in response.data]
        summary = summarize(points_from_events(events))
        evidence = await self._evidence(
            st,
            response,
            EvidenceType.RUN_HISTORY,
            {
                "refreshes": [
                    {"refresh_id": e.payload.get("refresh_id"), "status": e.payload.get("status"), "at": e.timestamp}
                    for e in sorted(events, key=lambda e: e.timestamp, reverse=True)[:10]
                ],
                "last_success_at": summary.last_success_at,
                "consecutive_failures": summary.consecutive_failures,
            },
            summary=f"{len(events)} refresh event(s) in the {_HISTORY_WINDOW.days} days before the failure; "
            f"{summary.consecutive_failures} consecutive failure(s)",
            resource_id=incident.primary_item_id,
            resource_type=ItemType.SEMANTIC_MODEL.value,
            observed_at=summary.last_success_at,
            # the history is the same source's events, read back from the store: not a second source
            source=trigger.source if isinstance(trigger := st.scratch.get("refresh_event"), OperationalEvent) else None,
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
        event = st.scratch.get("refresh_event")
        message = event.payload.get("error_message") if isinstance(event, OperationalEvent) else None
        sources = [(message, None)] if isinstance(message, str) and message else []
        await self._record_errors(
            st,
            sources,
            run_source=event.source if isinstance(event, OperationalEvent) else "fabric.workspace-monitoring",
            run_resource_type=ItemType.SEMANTIC_MODEL.value,
            run_label="Refresh",
        )
