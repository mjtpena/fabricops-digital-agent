"""Scheduled SLA sweep: evaluate every declared SLA and record state changes as operational events.

The sweep only *observes*. It appends one ``SLA_ASSESSED`` event each time an SLA reaches a new state for a given
deadline day (``ON_TRACK`` -> ``AT_RISK`` -> ``BREACHED`` -> ...), so the event store holds a compact history and a
detection rule sees each transition exactly once. Opening an incident for a breach is a detection rule's job (a later
workstream adds it): the rule matches ``event_type == SLA_ASSESSED`` and ``payload["state"] == "BREACHED"`` and finds
everything it needs in the payload, which is the full :class:`~orqentis.domain.sla.SLAAssessment` plus a ``declared``
object (what the contracts say about the data product and the item that refreshes it: environment, ``criticality``).

**A breach needs fresh telemetry.** "Not refreshed since ..." is only true if FabricOps could see the refresh. When the
collector that observes the freshness item has no recent watermark (it has been failing, or was never able to run), the
sweep does not declare a breach: the assessment still returns to the caller, and the event is withheld (and a warning
logged) until the telemetry is current again. A breach is therefore never declared from blindness (ADR-0013 open
question 3, ADR-0020).
"""

from __future__ import annotations

from datetime import timedelta

from orqentis.config.clock import Clock
from orqentis.domain.common import Environment, ItemType
from orqentis.domain.events import EventSeverity, EventType, OperationalEvent
from orqentis.domain.incidents import Incident, IncidentStatus
from orqentis.domain.sla import SLA, SLAAssessment, SLAState
from orqentis.incidents.detection_runner import DetectionRunner
from orqentis.incidents.sla import SLAEvaluator, is_sla_incident
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


# The collectors that observe a freshness item, by item type. Semantic models are observed by whichever refresh source
# is configured; the second name is the workspace-scoped one (its scope is the workspace id, not the model id).
_FRESHNESS_COLLECTORS: dict[ItemType, tuple[str, ...]] = {
    ItemType.DATA_PIPELINE: ("pipeline-runs",),
    ItemType.NOTEBOOK: ("notebook-runs",),
    ItemType.SPARK_JOB_DEFINITION: ("spark-job-runs",),
    ItemType.DATAFLOW: ("dataflow-runs",),
    ItemType.SEMANTIC_MODEL: ("semantic-model-refreshes", "workspace-monitoring-refreshes"),
}


def sla_event(sla: SLA, assessment: SLAAssessment, *, catalog: EstateCatalog) -> OperationalEvent:
    """The event recording ``assessment``. One event per (SLA, deadline day, state): the dedupe key says so."""
    assert assessment.deadline is not None
    day = assessment.deadline.date().isoformat()
    node = catalog.node(sla.freshness_item_id)
    product = catalog.data_products.get(sla.data_product_id)
    criticality = product.metadata.get("criticality") if product else None
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
        payload={
            **assessment.model_dump(mode="json"),
            "declared": {
                "data_product_id": sla.data_product_id,
                "criticality": criticality if isinstance(criticality, str) else None,
                "freshness_item_name": node.display_name if node else None,
            },
        },
    )


class SLASweeper:
    def __init__(
        self,
        catalog: EstateCatalog,
        evaluator: SLAEvaluator,
        graph: DependencyGraph,
        repository: IncidentRepository,
        event_store: EventStore,
        detection: DetectionRunner,
        clock: Clock,
        *,
        max_telemetry_lag: timedelta | None = None,
    ) -> None:
        self._catalog = catalog
        self._evaluator = evaluator
        self._graph = graph
        self._repository = repository
        self._events = event_store
        self._detection = detection
        self._clock = clock
        # None: do not check (tests and mock mode). Otherwise a breach is declared only if the collector that observes
        # the freshness item completed a pass for it within this long.
        self._max_lag = max_telemetry_lag

    async def _telemetry_is_current(self, sla: SLA) -> bool:
        if self._max_lag is None:
            return True
        node = self._catalog.node(sla.freshness_item_id)
        collectors = _FRESHNESS_COLLECTORS.get(node.item_type) if node else None
        if node is None or collectors is None:
            return True  # nothing we collect observes this item type, so there is no watermark to wait for
        now = self._clock.now()
        for name in collectors:
            marks = await self._events.get_watermarks(name)
            # a semantic model is a scope of the Power BI collector, or (by workspace) of workspace monitoring
            scope = (node.workspace_id or "") if name == "workspace-monitoring-refreshes" else sla.freshness_item_id
            if (mark := marks.get(scope)) is not None and now - mark <= self._max_lag:
                return True
        return False

    async def sweep(self) -> tuple[list[SLAAssessment], list[Incident]]:
        """Assess every SLA; returns the assessments and any incidents the detection rules opened from them."""
        # An SLA incident is about a missed deadline: it does not block the refresh it reports on.
        open_incidents = [
            i
            for i in await self._repository.list(
                IncidentQuery(statuses=[s for s in IncidentStatus if s.is_open], limit=1000)
            )
            if not is_sla_incident(i)
        ]
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
            if assessment.state is SLAState.BREACHED and not await self._telemetry_is_current(sla):
                _log.warning(
                    "sla breach withheld: the collector that observes its freshness item has no recent watermark",
                    extra={"sla_id": sla.id, "freshness_item_id": sla.freshness_item_id},
                )
                continue
            events.append(sla_event(sla, assessment, catalog=self._catalog))
        new_events = await self._events.append(events)
        created = await self._detection.run()  # reads the new events (and anything else pending) from the cursor
        _log.info(
            "sla sweep",
            extra={
                "slas": len(assessments),
                "state_changes": len(new_events),
                "incidents_created": [i.incident_id for i in created],
            },
        )
        return assessments, created
