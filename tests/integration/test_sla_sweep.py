"""Scheduled SLA sweep: state changes are recorded as operational events a detection rule can consume."""

from __future__ import annotations

from datetime import timedelta

from orqentis.bootstrap import Container
from orqentis.config.clock import FixedClock
from orqentis.domain.events import EventSeverity, EventType, OperationalEvent
from orqentis.domain.sla import SLAState
from orqentis.domain.taxonomy import IncidentCategory, Severity
from orqentis.incidents.detection import DetectionEngine
from orqentis.incidents.service import IncidentCandidate
from orqentis.incidents.sla import SLAEvaluator
from orqentis.incidents.sla_sweep import SLASweeper
from orqentis.storage.event_store import EventQuery
from tests.conftest import detect


async def sla_events(container: Container) -> list[OperationalEvent]:
    return await container.event_store.query(EventQuery(event_types=[EventType.SLA_ASSESSED]))


async def test_the_sweep_records_each_sla_state_once_per_deadline_day(container: Container) -> None:
    clock = container.clock
    assert isinstance(clock, FixedClock)
    incident = await detect(container)  # the failed sales pipeline blocks today's refresh

    assessments, _ = await container.sla_sweeper.sweep()
    assert {a.sla_id: a.state for a in assessments} == {
        "sla-sales-gold-0630": SLAState.AT_RISK,
        "sla-gl-daily-0700": SLAState.MET,
        "sla-inventory-0700": SLAState.ON_TRACK,
    }
    events = {e.payload["sla_id"]: e for e in await sla_events(container)}
    assert len(events) == 3

    at_risk = events["sla-sales-gold-0630"]
    assert at_risk.severity is EventSeverity.WARNING and at_risk.source == "orqentis.sla"
    assert at_risk.dedupe_key == "SLA_ASSESSED:sla-sales-gold-0630:2026-10-05:AT_RISK"
    assert at_risk.correlation_id == "sla:sla-sales-gold-0630:2026-10-05"
    assert (at_risk.item_id, at_risk.workspace_id) == ("pl-ingest-sales-prod", "ws-sales-prod")
    # The payload is the full assessment: everything a rule needs, nothing it has to look up.
    assert at_risk.payload["state"] == "AT_RISK" and at_risk.payload["blocking_item_ids"] == [incident.primary_item_id]
    assert at_risk.payload["owner"] and at_risk.payload["deadline"] and at_risk.payload["data_product_id"]

    # Sweeping again changes nothing: the event store is a history of transitions, not of sweeps.
    await container.sla_sweeper.sweep()
    assert len(await sla_events(container)) == 3

    # An hour later the deadlines have passed: the unfinished SLAs are recorded as BREACHED, MET stays as it was.
    clock.advance(timedelta(hours=1))
    await container.sla_sweeper.sweep()
    events_after = await sla_events(container)
    assert len(events_after) == 5
    breached = {e.payload["sla_id"] for e in events_after if e.payload["state"] == "BREACHED"}
    assert breached == {"sla-sales-gold-0630", "sla-inventory-0700"}
    assert all(e.severity is EventSeverity.ERROR for e in events_after if e.payload["state"] == "BREACHED")


class SlaBreachRule:
    """What a later workstream's rule looks like: it only needs the recorded event."""

    rule_id = "TEST-SLA-BREACH"

    def evaluate(self, event: OperationalEvent) -> IncidentCandidate | None:
        if event.event_type is not EventType.SLA_ASSESSED or event.payload.get("state") != "BREACHED":
            return None
        assert event.item_id and event.workspace_id and event.correlation_id
        return IncidentCandidate(
            title=f"SLA breached: {event.display_name}",
            category=IncidentCategory.SLA_BREACH,
            severity=Severity.HIGH,
            tenant_id=event.tenant_id,
            workspace_id=event.workspace_id,
            primary_item_id=event.item_id,
            primary_item_type=event.item_type,
            primary_item_name=event.display_name,
            environment=event.environment,
            correlation_id=event.correlation_id,
            detected_at=event.timestamp,
            source=event.source,
            rule_id=self.rule_id,
            trigger_event_id=event.event_id,
        )


async def test_a_detection_rule_can_open_incidents_from_the_recorded_sla_events(container: Container) -> None:
    clock = container.clock
    assert isinstance(clock, FixedClock)
    sweeper = SLASweeper(
        container.catalog,
        SLAEvaluator(container.event_store, clock),
        container.graph,
        container.incidents.repository,
        container.event_store,
        DetectionEngine(container.incidents, clock, rules=[SlaBreachRule()]),
        clock,
    )
    _, created = await sweeper.sweep()
    assert created == []  # nothing is breached yet

    clock.advance(timedelta(hours=1))
    _, created = await sweeper.sweep()
    assert {i.category for i in created} == {IncidentCategory.SLA_BREACH}
    assert {i.metadata["detection_rule"] for i in created} == {"TEST-SLA-BREACH"}  # a rule opened it, not the sweep

    _, again = await sweeper.sweep()
    assert again == []  # each breach is seen once


async def test_the_default_rules_ignore_sla_events(container: Container) -> None:
    clock = container.clock
    assert isinstance(clock, FixedClock)
    clock.advance(timedelta(hours=1))
    before = len(await container.incidents.repository.list())
    _, created = await container.sla_sweeper.sweep()
    assert created == [] and len(await container.incidents.repository.list()) == before
    assert len(await sla_events(container)) >= 1
