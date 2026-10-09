"""Investigation service: runs the right workflow for an incident and persists the trace."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from uuid import uuid4

from orqentis.config.clock import Clock
from orqentis.domain.incidents import Incident, IncidentStatus
from orqentis.domain.principals import Principal
from orqentis.incidents.service import IncidentService
from orqentis.incidents.workflows.base import InvestigationTrace, InvestigationWorkflow
from orqentis.observability.audit import AuditTrail
from orqentis.observability.logging import get_logger, log_context
from orqentis.observability.tracing import get_tracer, span_trace_id
from orqentis.storage.evidence_store import EvidenceStore

_log = get_logger(__name__)

INVESTIGABLE = frozenset(
    {
        IncidentStatus.DETECTED,
        IncidentStatus.DIAGNOSED,
        IncidentStatus.AWAITING_APPROVAL,
        IncidentStatus.RESOLVED,
    }
)


class InvestigationNotPossibleError(Exception):
    pass


class InvestigationService:
    def __init__(
        self,
        workflows: Sequence[InvestigationWorkflow],
        incidents: IncidentService,
        evidence_store: EvidenceStore,
        audit: AuditTrail,
        clock: Clock,
    ) -> None:
        self._workflows = list(workflows)
        self._incidents = incidents
        self._store = evidence_store
        self._audit = audit
        self._clock = clock
        self._locks: dict[str, asyncio.Lock] = {}

    def workflow_for(self, incident: Incident) -> InvestigationWorkflow | None:
        return next((w for w in self._workflows if w.supports(incident)), None)

    async def investigate(self, incident_id: str, *, actor: Principal) -> tuple[Incident, InvestigationTrace]:
        lock = self._locks.setdefault(incident_id, asyncio.Lock())
        if lock.locked():
            raise InvestigationNotPossibleError(f"Incident {incident_id} is already being investigated")
        async with lock:
            incident = await self._incidents.get(incident_id)
            if incident.status not in INVESTIGABLE:
                raise InvestigationNotPossibleError(
                    f"Incident {incident_id} is {incident.status.value}; cannot investigate"
                )
            workflow = self.workflow_for(incident)
            if workflow is None:
                raise InvestigationNotPossibleError(
                    f"No investigation workflow for {incident.primary_item_type} incidents yet"
                )
            with get_tracer().start_as_current_span("investigation") as span:
                trace_id = span_trace_id(span) or uuid4().hex
                span.set_attribute("orqentis.incident_id", incident_id)
                span.set_attribute("orqentis.workflow", workflow.name)
                trace = InvestigationTrace(
                    trace_id=trace_id, incident_id=incident_id, workflow=workflow.name, started_at=self._clock.now()
                )
                await self._audit.record(
                    actor=actor.id,
                    action="investigation.start",
                    outcome="STARTED",
                    incident_id=incident_id,
                    trace_id=trace_id,
                    details={"workflow": workflow.name},
                )
                with log_context(trace_id=trace_id, incident_id=incident_id):
                    try:
                        incident = await workflow.run(incident, trace=trace)
                    except Exception as exc:
                        trace.errors.append(f"{type(exc).__name__}: {exc}")
                        await self._abort(incident, actor, exc)
                        raise
                    finally:
                        trace.completed_at = self._clock.now()
                        await self._store.put_json(incident_id, f"trace-{trace_id}.json", trace.model_dump(mode="json"))
                await self._audit.record(
                    actor=actor.id,
                    action="investigation.complete",
                    outcome=incident.status.value,
                    incident_id=incident_id,
                    trace_id=trace_id,
                    details={
                        "category": incident.category,
                        "confidence": incident.confidence.score if incident.confidence else None,
                        "tool_calls": len(trace.tool_calls),
                        "approvals": trace.approval_ids,
                        "report": incident.report_uri,
                    },
                )
                _log.info("investigation complete", extra={"incident_id": incident_id, "status": incident.status.value})
                return incident, trace

    async def _abort(self, incident: Incident, actor: Principal, exc: Exception) -> None:
        _log.exception("investigation failed", extra={"incident_id": incident.incident_id})
        current = await self._incidents.repository.get(incident.incident_id)
        if current is not None and current.status is IncidentStatus.INVESTIGATING:
            await self._incidents.transition(
                current, IncidentStatus.DETECTED, actor=actor.id, reason=f"Investigation failed: {type(exc).__name__}"
            )
