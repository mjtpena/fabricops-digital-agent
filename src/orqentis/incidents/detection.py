"""Detection rules: deterministic conditions that open incidents from operational events."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from typing import Protocol

from orqentis.config.clock import Clock
from orqentis.domain.common import Environment, ItemType
from orqentis.domain.events import EventType, OperationalEvent
from orqentis.domain.incidents import Incident, Symptom, TimelineKind
from orqentis.domain.taxonomy import IncidentCategory, Severity
from orqentis.incidents.service import IncidentCandidate, IncidentService
from orqentis.incidents.sla import SLA_RULE_ID
from orqentis.observability.logging import get_logger
from orqentis.storage.event_store import EventQuery, EventStore
from orqentis.storage.incidents import DuplicateIncidentError, IncidentQuery

_log = get_logger(__name__)

_SEVERITY_BY_ENVIRONMENT = {
    Environment.PROD: Severity.HIGH,
    Environment.TEST: Severity.MEDIUM,
    Environment.DEV: Severity.LOW,
    Environment.UNKNOWN: Severity.MEDIUM,
}


class DetectionRule(Protocol):
    rule_id: str

    def evaluate(self, event: OperationalEvent) -> IncidentCandidate | None: ...


def _symptom(event: OperationalEvent) -> Symptom:
    return Symptom(
        description=event.summary,
        observed_at=event.timestamp,
        source=event.source,
        item_id=event.item_id,
        event_id=event.event_id,
    )


class PipelineFailureRule:
    rule_id = "DET-PIPELINE-FAILED"

    def evaluate(self, event: OperationalEvent) -> IncidentCandidate | None:
        if event.event_type is not EventType.PIPELINE_RUN_FAILED or not event.item_id or not event.workspace_id:
            return None
        name = event.display_name or event.item_id
        return IncidentCandidate(
            title=f"Pipeline {name} failed",
            category=IncidentCategory.PIPELINE_FAILURE,
            severity=_SEVERITY_BY_ENVIRONMENT[event.environment],
            tenant_id=event.tenant_id,
            workspace_id=event.workspace_id,
            primary_item_id=event.item_id,
            primary_item_type=ItemType.DATA_PIPELINE,
            primary_item_name=name,
            environment=event.environment,
            correlation_id=event.correlation_id,
            detected_at=event.timestamp,
            source=event.source,
            rule_id=self.rule_id,
            trigger_event_id=event.event_id,
            symptoms=[_symptom(event)],
        )


class _ItemJobFailureRule:
    """A notebook, Spark job definition or Dataflow job that failed on its own (ADR-0020).

    A job that another item started (a pipeline's notebook activity) is the launcher's failure, not a second
    incident: the event says so (``launched_by_item``), and the engine also drops a candidate whose failure a pipeline
    incident already covers (``covered_by_activity_target``).
    """

    rule_id: str
    event_type: EventType
    item_type: ItemType
    noun: str

    def evaluate(self, event: OperationalEvent) -> IncidentCandidate | None:
        if event.event_type is not self.event_type or not event.item_id or not event.workspace_id:
            return None
        if event.payload.get("launched_by_item") is True:
            return None
        name = event.display_name or event.item_id
        return IncidentCandidate(
            title=f"{self.noun} {name} failed",
            category=IncidentCategory.ACTIVITY_FAILURE,
            severity=_SEVERITY_BY_ENVIRONMENT[event.environment],
            tenant_id=event.tenant_id,
            workspace_id=event.workspace_id,
            primary_item_id=event.item_id,
            primary_item_type=self.item_type,
            primary_item_name=name,
            environment=event.environment,
            correlation_id=event.correlation_id,
            detected_at=event.timestamp,
            source=event.source,
            rule_id=self.rule_id,
            trigger_event_id=event.event_id,
            symptoms=[_symptom(event)],
            covered_by_activity_target=event.item_id,
        )


class NotebookFailureRule(_ItemJobFailureRule):
    rule_id = "DET-NOTEBOOK-FAILED"
    event_type = EventType.NOTEBOOK_RUN_FAILED
    item_type = ItemType.NOTEBOOK
    noun = "Notebook"


class SparkJobFailureRule(_ItemJobFailureRule):
    rule_id = "DET-SPARK-JOB-FAILED"
    event_type = EventType.SPARK_JOB_FAILED
    item_type = ItemType.SPARK_JOB_DEFINITION
    noun = "Spark job definition"


class DataflowFailureRule(_ItemJobFailureRule):
    rule_id = "DET-DATAFLOW-FAILED"
    event_type = EventType.DATAFLOW_REFRESH_FAILED
    item_type = ItemType.DATAFLOW
    noun = "Dataflow"


class RefreshFailureRule:
    """A semantic model refresh that failed (workspace monitoring, or the Power BI history where it is readable)."""

    rule_id = "DET-REFRESH-FAILED"

    def evaluate(self, event: OperationalEvent) -> IncidentCandidate | None:
        if event.event_type is not EventType.REFRESH_FAILED or not event.item_id or not event.workspace_id:
            return None
        name = event.display_name or event.item_id
        return IncidentCandidate(
            title=f"Semantic model {name} refresh failed",
            category=IncidentCategory.SEMANTIC_MODEL_REFRESH_FAILURE,
            severity=_SEVERITY_BY_ENVIRONMENT[event.environment],
            tenant_id=event.tenant_id,
            workspace_id=event.workspace_id,
            primary_item_id=event.item_id,
            primary_item_type=ItemType.SEMANTIC_MODEL,
            primary_item_name=name,
            environment=event.environment,
            correlation_id=event.correlation_id,
            detected_at=event.timestamp,
            source=event.source,
            rule_id=self.rule_id,
            trigger_event_id=event.event_id,
            symptoms=[_symptom(event)],
            # A pipeline's refresh activity failing at the same time is the same failure seen from the pipeline.
            covered_by_activity_target=event.item_id,
        )


# What a data product's owner may declare in the contracts (``metadata.criticality``) to set the severity of a breach.
_CRITICALITY = {s.value.lower(): s for s in Severity}
_SLA_SEVERITY_BY_ENVIRONMENT = {
    Environment.PROD: Severity.CRITICAL,
    Environment.TEST: Severity.MEDIUM,
    Environment.DEV: Severity.LOW,
    Environment.UNKNOWN: Severity.MEDIUM,
}


class SlaBreachRule:
    """A data product SLA the sweep found ``BREACHED`` (it writes one ``SLA_ASSESSED`` event per SLA, day and state).

    Severity comes from what the owner declared about the data product (``metadata.criticality``: critical, high,
    medium or low) and otherwise from the environment of the item that is supposed to refresh it. One incident per SLA
    and deadline day: the event's correlation id is ``sla:<id>:<date>``.
    """

    rule_id = SLA_RULE_ID

    def evaluate(self, event: OperationalEvent) -> IncidentCandidate | None:
        if event.event_type is not EventType.SLA_ASSESSED or event.payload.get("state") != "BREACHED":
            return None
        if not event.item_id or not event.workspace_id or not event.correlation_id:
            return None
        declared = event.payload.get("declared")
        declared = declared if isinstance(declared, dict) else {}
        criticality = declared.get("criticality")
        severity = (
            _CRITICALITY.get(criticality.strip().lower()) if isinstance(criticality, str) else None
        ) or _SLA_SEVERITY_BY_ENVIRONMENT[event.environment]
        product = str(event.payload.get("data_product_name") or event.display_name or event.item_id)
        item_name = declared.get("freshness_item_name")
        return IncidentCandidate(
            title=f"SLA breached: {product}",
            category=IncidentCategory.SLA_BREACH,
            severity=severity,
            tenant_id=event.tenant_id,
            workspace_id=event.workspace_id,
            primary_item_id=event.item_id,
            primary_item_type=event.item_type,
            primary_item_name=item_name if isinstance(item_name, str) else None,
            environment=event.environment,
            correlation_id=event.correlation_id,
            detected_at=event.timestamp,
            source=event.source,
            rule_id=self.rule_id,
            trigger_event_id=event.event_id,
            symptoms=[_symptom(event)],
        )


DEFAULT_RULES: tuple[DetectionRule, ...] = (
    PipelineFailureRule(),
    NotebookFailureRule(),
    SparkJobFailureRule(),
    DataflowFailureRule(),
    RefreshFailureRule(),
    SlaBreachRule(),
)

# A failed pipeline activity and the job it started finish within seconds of each other; this is generous for clock
# skew and slow polling, and short enough that a later, unrelated failure of the same item is still its own incident.
COVERAGE_WINDOW = timedelta(minutes=10)


class DetectionEngine:
    """Evaluates new events against rules; de-duplicates by (item, correlation)."""

    def __init__(
        self,
        incidents: IncidentService,
        clock: Clock,
        *,
        rules: Sequence[DetectionRule] = DEFAULT_RULES,
        window: timedelta = timedelta(hours=6),
        events: EventStore | None = None,
    ) -> None:
        self._incidents = incidents
        self._clock = clock
        self._rules = list(rules)
        self._window = window
        # Lets an incident find the activity failures that were stored before it (or in an earlier batch).
        self._events = events

    async def process(self, events: Sequence[OperationalEvent]) -> list[Incident]:
        created: list[Incident] = []
        horizon = self._clock.now() - self._window
        # Historical events are context only: nobody is paged for old failures.
        recent = sorted((e for e in events if e.timestamp >= horizon), key=lambda e: e.timestamp)
        # Pass 1 opens incidents; pass 2 attaches activity-level symptoms (which can precede the run event).
        for event in recent:
            if event.event_type is EventType.ACTIVITY_RUN_FAILED:
                continue
            for rule in self._rules:
                candidate = rule.evaluate(event)
                if candidate is None:
                    continue
                existing = (
                    await self._incidents.repository.find_by_correlation(
                        candidate.primary_item_id, candidate.correlation_id
                    )
                    if candidate.correlation_id
                    else await self._opened_by(candidate)
                )
                if existing is not None:
                    _log.info(
                        "duplicate detection suppressed",
                        extra={"incident_id": existing.incident_id, "event_id": event.event_id},
                    )
                    await self._attach_stored_activity_failures(existing)
                    continue
                if await self._covered_by_pipeline_failure(candidate):
                    _log.info(
                        "detection suppressed: a failed pipeline activity covers this failure",
                        extra={
                            "rule_id": rule.rule_id,
                            "event_id": event.event_id,
                            "item_id": candidate.primary_item_id,
                        },
                    )
                    continue
                try:
                    incident = await self._incidents.create(candidate)
                except DuplicateIncidentError as exc:
                    # The store's backstop: another pass opened it in the meantime (the mutex was lost, or a very
                    # unlucky interleaving). Not an error and not a new incident.
                    _log.warning(
                        "duplicate detection suppressed by the store",
                        extra={
                            "event_id": event.event_id,
                            "incident_id": exc.existing.incident_id if exc.existing else None,
                        },
                    )
                    if exc.existing is not None:
                        await self._attach_stored_activity_failures(exc.existing)
                    continue
                created.append(incident)
                await self._attach_stored_activity_failures(incident)
        for event in recent:
            if event.event_type is EventType.ACTIVITY_RUN_FAILED:
                await self._attach_symptom(event)
        return [await self._incidents.repository.get(i.incident_id) or i for i in created]

    async def _covered_by_pipeline_failure(self, candidate: IncidentCandidate) -> bool:
        """Whether a failed pipeline activity that targets the item, at about this time, already explains the failure.

        A pipeline that runs a notebook (or refreshes a model, or runs a Dataflow) fails *because* that item failed, and
        its incident already names the item as the failure point. A second incident for the same failure would page
        twice. The match is the activity's target item and the time, never a name, and needs the event store.
        """
        target = candidate.covered_by_activity_target
        if target is None or self._events is None:
            return False
        stored = await self._events.query(
            EventQuery(
                event_types=[EventType.ACTIVITY_RUN_FAILED],
                since=candidate.detected_at - COVERAGE_WINDOW,
                until=candidate.detected_at + COVERAGE_WINDOW,
                limit=10_000,
            )
        )
        return any(e.payload.get("target_item_id") == target for e in stored)

    async def _opened_by(self, candidate: IncidentCandidate) -> Incident | None:
        """The incident an earlier pass already opened for this very event (candidates without a correlation id).

        Detection is fed from a durable cursor, so after an interruption the same events are processed again; this
        keeps that harmless for rules that have no correlation id to de-duplicate on.
        """
        recent = await self._incidents.repository.list(IncidentQuery(primary_item_id=candidate.primary_item_id))
        return next((i for i in recent if i.metadata.get("trigger_event_id") == candidate.trigger_event_id), None)

    async def _attach_stored_activity_failures(self, incident: Incident) -> None:
        """Attach the activity failures of this run that are already in the event store.

        The collector stores a failed run's activity failures *before* the run's own failure event, and detection reads
        the store in batches: the activity failure can sit in an earlier batch (or an earlier, interrupted pass) than
        the failure that opens the incident. Looking them up here means the symptoms never depend on batch boundaries.
        """
        if self._events is None or not incident.correlation_id:
            return
        stored = await self._events.query(
            EventQuery(
                item_ids=[incident.primary_item_id],
                event_types=[EventType.ACTIVITY_RUN_FAILED],
                correlation_id=incident.correlation_id,
            )
        )
        current = await self._incidents.repository.get(incident.incident_id) or incident
        known = {s.event_id for s in current.symptoms}
        fresh = [e for e in stored if e.event_id not in known]
        if not fresh:
            return
        for event in fresh:
            current.symptoms.append(_symptom(event))
            self._incidents.add_timeline(
                current, TimelineKind.TELEMETRY, event.summary, actor="detection", timestamp=event.timestamp
            )
        await self._incidents.save(current)

    async def _attach_symptom(self, event: OperationalEvent) -> None:
        if not event.item_id or not event.correlation_id:
            return
        incident = await self._incidents.repository.find_by_correlation(event.item_id, event.correlation_id)
        if incident is None or any(s.event_id == event.event_id for s in incident.symptoms):
            return
        incident.symptoms.append(_symptom(event))
        self._incidents.add_timeline(
            incident, TimelineKind.TELEMETRY, event.summary, actor="detection", timestamp=event.timestamp
        )
        await self._incidents.save(incident)
