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
from orqentis.observability.logging import get_logger

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


DEFAULT_RULES: tuple[DetectionRule, ...] = (PipelineFailureRule(),)


class DetectionEngine:
    """Evaluates new events against rules; de-duplicates by (item, correlation)."""

    def __init__(
        self,
        incidents: IncidentService,
        clock: Clock,
        *,
        rules: Sequence[DetectionRule] = DEFAULT_RULES,
        window: timedelta = timedelta(hours=6),
    ) -> None:
        self._incidents = incidents
        self._clock = clock
        self._rules = list(rules)
        self._window = window

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
                    else None
                )
                if existing is not None:
                    _log.info(
                        "duplicate detection suppressed",
                        extra={"incident_id": existing.incident_id, "event_id": event.event_id},
                    )
                    continue
                created.append(await self._incidents.create(candidate))
        for event in recent:
            if event.event_type is EventType.ACTIVITY_RUN_FAILED:
                await self._attach_symptom(event)
        return [await self._incidents.repository.get(i.incident_id) or i for i in created]

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
