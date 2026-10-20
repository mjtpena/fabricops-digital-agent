"""Scheduled SLA sweep: evaluate every declared SLA and record state changes as operational events.

The sweep only *observes*. It appends one ``SLA_ASSESSED`` event each time an SLA reaches a new state for a given
deadline day (``ON_TRACK`` -> ``AT_RISK`` -> ``BREACHED`` -> ...), so the event store holds a compact history and a
detection rule sees each transition exactly once. Opening an incident for a breach is a detection rule's job (a later
workstream adds it): the rule matches ``event_type == SLA_ASSESSED`` and ``payload["state"] == "BREACHED"`` and finds
everything it needs in the payload, which is the full :class:`~orqentis.domain.sla.SLAAssessment`.
"""

from __future__ import annotations

from orqentis.config.clock import Clock
from orqentis.domain.common import Environment
from orqentis.domain.events import EventSeverity, EventType, OperationalEvent
from orqentis.domain.incidents import Incident, IncidentStatus
from orqentis.domain.sla import SLA, SLAAssessment, SLAState
from orqentis.incidents.detection import DetectionEngine
from orqentis.incidents.sla import SLAEvaluator
from orqentis.observability.logging import get_logger
from orqentis.storage.event_store import EventStore
from orqentis.storage.incidents import IncidentQuery, IncidentRepository
from orqentis.topology.catalog import EstateCatalog
from orqentis.topology.graph import DependencyGraph

_log = get_logger(__name__)

SLA_SOURCE = "orqentis.sla"

_SEVERITY = {
    SLAState.BREACHED: EventSeverity.ERROR,
    SLAState.AT_RISK: EventSeverity.WARNING,
}


def sla_event(sla: SLA, assessment: SLAAssessment, *, catalog: EstateCatalog) -> OperationalEvent:
    """The event recording ``assessment``. One event per (SLA, deadline day, state): the dedupe key says so."""
    assert assessment.deadline is not None
    day = assessment.deadline.date().isoformat()
    node = catalog.node(sla.freshness_item_id)
    return OperationalEvent(
        event_id=f"evt-sla-{sla.id}-{day}-{assessment.state.value.lower()}",
        event_type=EventType.SLA_ASSESSED,
        tenant_id=sla.tenant_id,
        workspace_id=node.workspace_id if node else None,
        item_id=sla.freshness_item_id,
        item_type=node.item_type if node else None,
        display_name=assessment.data_product_name,
        environment=node.environment if node else Environment.UNKNOWN,
        timestamp=assessment.evaluated_at,
        severity=_SEVERITY.get(assessment.state, EventSeverity.INFO),
        summary=f"SLA '{sla.display_name}' is {assessment.state.value}: {assessment.reason}",
        correlation_id=f"sla:{sla.id}:{day}",
        dedupe_key=f"{EventType.SLA_ASSESSED.value}:{sla.id}:{day}:{assessment.state.value}",
        source=SLA_SOURCE,
        payload=assessment.model_dump(mode="json"),
    )


class SLASweeper:
    def __init__(
        self,
        catalog: EstateCatalog,
        evaluator: SLAEvaluator,
        graph: DependencyGraph,
        repository: IncidentRepository,
        event_store: EventStore,
        detection: DetectionEngine,
        clock: Clock,
    ) -> None:
        self._catalog = catalog
        self._evaluator = evaluator
        self._graph = graph
        self._repository = repository
        self._events = event_store
        self._detection = detection
        self._clock = clock

    async def sweep(self) -> tuple[list[SLAAssessment], list[Incident]]:
        """Assess every SLA; returns the assessments and any incidents the detection rules opened from them."""
        open_incidents = await self._repository.list(
            IncidentQuery(statuses=[s for s in IncidentStatus if s.is_open], limit=1000)
        )
        assessments: list[SLAAssessment] = []
        events: list[OperationalEvent] = []
        for sla in sorted(self._catalog.slas.values(), key=lambda s: s.id):
            data_product = self._catalog.data_products.get(sla.data_product_id)
            blocking = sorted(
                {
                    i.primary_item_id
                    for i in open_incidents
                    if i.primary_item_id == sla.freshness_item_id
                    or self._graph.find_path(i.primary_item_id, sla.freshness_item_id) is not None
                }
            )
            assessment = await self._evaluator.assess(
                sla,
                data_product_name=data_product.display_name if data_product else sla.data_product_id,
                blocking_item_ids=blocking,
            )
            assessments.append(assessment)
            if assessment.state is SLAState.NOT_APPLICABLE:
                continue  # nothing to observe today
            events.append(sla_event(sla, assessment, catalog=self._catalog))
        new_events = await self._events.append(events)
        created = await self._detection.process(new_events)
        _log.info(
            "sla sweep",
            extra={
                "slas": len(assessments),
                "state_changes": len(new_events),
                "incidents_created": [i.incident_id for i in created],
            },
        )
        return assessments, created
