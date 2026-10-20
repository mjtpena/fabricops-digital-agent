"""Incident lifecycle: creation, validated status transitions, timeline and persistence."""

from __future__ import annotations

from typing import Any

from pydantic import AwareDatetime, BaseModel, Field

from orqentis.config.clock import Clock
from orqentis.domain.common import Environment, ItemType
from orqentis.domain.events import EventSeverity, EventType, OperationalEvent
from orqentis.domain.incidents import Incident, IncidentStatus, Symptom, TimelineEntry, TimelineKind
from orqentis.domain.taxonomy import CategoryName, Severity
from orqentis.notifications import events as notification_events
from orqentis.notifications.sink import NOOP_SINK, NotificationSink
from orqentis.observability.audit import AuditTrail
from orqentis.observability.logging import get_logger
from orqentis.observability.metrics import DISABLED, Metrics
from orqentis.storage.event_store import EventStore
from orqentis.storage.incidents import IncidentRepository

_log = get_logger(__name__)


class InvalidTransitionError(Exception):
    def __init__(self, incident_id: str, current: IncidentStatus, target: IncidentStatus) -> None:
        super().__init__(f"Incident {incident_id}: transition {current.value} -> {target.value} is not allowed")
        self.current = current
        self.target = target


class IncidentNotFoundError(Exception):
    pass


class IncidentCandidate(BaseModel):
    """What a detection rule proposes; the service turns it into an :class:`Incident`."""

    title: str
    category: CategoryName
    severity: Severity
    tenant_id: str
    workspace_id: str
    primary_item_id: str
    primary_item_type: ItemType | None = None
    primary_item_name: str | None = None
    environment: Environment = Environment.UNKNOWN
    correlation_id: str | None = None
    detected_at: AwareDatetime
    source: str
    rule_id: str
    trigger_event_id: str
    symptoms: list[Symptom] = Field(default_factory=list)


class IncidentService:
    def __init__(
        self,
        repository: IncidentRepository,
        event_store: EventStore,
        audit: AuditTrail,
        clock: Clock,
        notifications: NotificationSink = NOOP_SINK,
        *,
        metrics: Metrics = DISABLED,
    ) -> None:
        self._repo = repository
        self._events = event_store
        self._audit = audit
        self._clock = clock
        self._notifications = notifications
        self._metrics = metrics

    @property
    def repository(self) -> IncidentRepository:
        return self._repo

    async def get(self, incident_id: str) -> Incident:
        incident = await self._repo.get(incident_id)
        if incident is None:
            raise IncidentNotFoundError(incident_id)
        return incident

    async def create(self, candidate: IncidentCandidate) -> Incident:
        now = self._clock.now()
        incident_id = await self._repo.next_incident_id(candidate.detected_at.year)
        incident = Incident(
            incident_id=incident_id,
            tenant_id=candidate.tenant_id,
            title=candidate.title,
            severity=candidate.severity,
            category=candidate.category,
            detected_at=candidate.detected_at,
            source=candidate.source,
            workspace_id=candidate.workspace_id,
            primary_item_id=candidate.primary_item_id,
            primary_item_type=candidate.primary_item_type,
            primary_item_name=candidate.primary_item_name,
            environment=candidate.environment,
            correlation_id=candidate.correlation_id,
            symptoms=list(candidate.symptoms),
            created_at=now,
            updated_at=now,
            metadata={"detection_rule": candidate.rule_id, "trigger_event_id": candidate.trigger_event_id},
        )
        self.add_timeline(
            incident,
            TimelineKind.DETECTED,
            f"Detected by rule {candidate.rule_id}: {candidate.title}",
            actor="detection",
            timestamp=candidate.detected_at,
        )
        await self._repo.save(incident)
        await self._emit(incident, EventType.INCIDENT_CREATED, f"Incident {incident_id} created: {incident.title}")
        await self._audit.record(
            actor="system:detection",
            action="incident.create",
            outcome="CREATED",
            incident_id=incident_id,
            details={
                "rule_id": candidate.rule_id,
                "category": incident.category,
                "severity": incident.severity.value,
                "trigger_event_id": candidate.trigger_event_id,
            },
        )
        _log.info("incident created", extra={"incident_id": incident_id, "category": incident.category})
        self._metrics.incident_opened(
            category=incident.category,
            severity=incident.severity,
            environment=incident.environment,
            # Failure time (the triggering event) to now: what the detection loop adds to the time the failure took.
            detection_latency_seconds=(now - candidate.detected_at).total_seconds(),
        )
        self._notifications.publish(notification_events.incident_opened(incident, occurred_at=now))
        return incident

    async def raise_severity(self, incident: Incident, severity: Severity, *, reason: str, actor: str) -> None:
        """Escalate an incident that is being investigated: timeline entry, saved, and only then announced.

        Announcing before the change is stored could tell people about a severity that a failing investigation then
        never persists (and the UI they are sent to would show the old one). One notification per incident and severity.
        """
        previous = incident.severity
        incident.severity = severity
        self.add_timeline(incident, TimelineKind.NOTE, f"Severity raised to {severity.value}: {reason}", actor=actor)
        await self.save(incident)
        self._notifications.publish(
            notification_events.incident_escalated(
                incident, previous=previous, reason=reason, occurred_at=self._clock.now()
            )
        )

    def add_timeline(
        self,
        incident: Incident,
        kind: TimelineKind,
        summary: str,
        *,
        actor: str,
        evidence_ids: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        timestamp: AwareDatetime | None = None,
    ) -> None:
        incident.timeline.append(
            TimelineEntry(
                timestamp=timestamp or self._clock.now(),
                kind=kind,
                summary=summary,
                actor=actor,
                evidence_ids=evidence_ids or [],
                metadata=metadata or {},
            )
        )

    async def transition(self, incident: Incident, target: IncidentStatus, *, actor: str, reason: str) -> Incident:
        if not incident.can_transition_to(target):
            raise InvalidTransitionError(incident.incident_id, incident.status, target)
        previous = incident.status
        incident.status = target
        self.add_timeline(
            incident, TimelineKind.STATUS_CHANGE, f"{previous.value} -> {target.value}: {reason}", actor=actor
        )
        await self.save(incident)
        await self._emit(
            incident,
            EventType.INCIDENT_STATUS_CHANGED,
            f"Incident {incident.incident_id} {previous.value} -> {target.value}",
        )
        await self._audit.record(
            actor=actor,
            action="incident.transition",
            outcome=target.value,
            incident_id=incident.incident_id,
            trace_id=incident.agent_trace_id,
            details={"from": previous.value, "to": target.value, "reason": reason},
        )
        return incident

    async def save(self, incident: Incident) -> None:
        incident.updated_at = self._clock.now()
        await self._repo.save(incident)

    async def _emit(self, incident: Incident, event_type: EventType, summary: str) -> None:
        now = self._clock.now()
        await self._events.append(
            [
                OperationalEvent(
                    event_id=f"evt-{event_type.value.lower()}-{incident.incident_id}-{int(now.timestamp() * 1000)}"
                    f"-{len(incident.timeline)}",
                    event_type=event_type,
                    tenant_id=incident.tenant_id,
                    workspace_id=incident.workspace_id,
                    item_id=incident.primary_item_id,
                    item_type=incident.primary_item_type,
                    display_name=incident.primary_item_name,
                    environment=incident.environment,
                    timestamp=now,
                    severity=EventSeverity.INFO,
                    summary=summary,
                    correlation_id=incident.incident_id,
                    dedupe_key=f"{event_type.value}:{incident.incident_id}:{incident.status.value}:{len(incident.timeline)}",
                    source="orqentis.incidents",
                    payload={"incident_id": incident.incident_id, "status": incident.status.value},
                )
            ]
        )
