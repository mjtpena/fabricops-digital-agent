"""Data product SLA evaluation from freshness events (deterministic)."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from orqentis.config.clock import Clock
from orqentis.domain.events import EventType
from orqentis.domain.incidents import Incident
from orqentis.domain.sla import SLA, SLAAssessment, SLAState
from orqentis.storage.event_store import EventQuery, EventStore

# The detection rule that opens an incident for a breached SLA (incidents/detection.py).
SLA_RULE_ID = "DET-SLA-BREACHED"


def is_sla_incident(incident: Incident) -> bool:
    """Opened by the SLA breach rule: about a missed deadline, not about a failure of the item that has to meet it."""
    return incident.metadata.get("detection_rule") == SLA_RULE_ID


class SLAEvaluator:
    def __init__(self, event_store: EventStore, clock: Clock) -> None:
        self._events = event_store
        self._clock = clock

    async def last_successful_update(self, sla: SLA, *, before: datetime) -> datetime | None:
        events = await self._events.query(
            EventQuery(
                item_ids=[sla.freshness_item_id],
                event_types=[EventType.PIPELINE_RUN_SUCCEEDED, EventType.REFRESH_SUCCEEDED],
                until=before,
                newest_first=True,
                limit=1,
            )
        )
        return events[0].timestamp if events else None

    async def assess(
        self, sla: SLA, *, data_product_name: str, blocking_item_ids: list[str] | None = None
    ) -> SLAAssessment:
        now = self._clock.now()
        tz = ZoneInfo(sla.timezone)
        local_now = now.astimezone(tz)
        deadline = datetime.combine(local_now.date(), sla.deadline_local_time, tzinfo=tz)
        blocking = blocking_item_ids or []

        def build(state: SLAState, reason: str, last: datetime | None) -> SLAAssessment:
            return SLAAssessment(
                sla_id=sla.id,
                data_product_id=sla.data_product_id,
                data_product_name=data_product_name,
                description=sla.description,
                state=state,
                deadline=deadline,
                evaluated_at=now,
                last_successful_update=last,
                minutes_to_deadline=round((deadline - now).total_seconds() / 60, 1),
                blocking_item_ids=blocking,
                owner=sla.owner,
                reason=reason,
            )

        if local_now.isoweekday() not in sla.applies_on_weekdays:
            return build(SLAState.NOT_APPLICABLE, "SLA does not apply today", None)

        last = await self.last_successful_update(sla, before=now)
        window_start = deadline - timedelta(days=1)
        if last is not None and window_start < last <= deadline:
            return build(SLAState.MET, f"Refreshed at {last.astimezone(tz):%H:%M %Z}, before the deadline", last)
        if now >= deadline:
            late = last is not None and last > deadline
            reason = (
                f"Refreshed late at {last.astimezone(tz):%H:%M %Z}"
                if late and last
                else f"Not refreshed since {last.astimezone(tz):%Y-%m-%d %H:%M %Z}"
                if last
                else "No successful refresh observed"
            )
            return build(SLAState.BREACHED, f"Deadline {deadline:%H:%M %Z} passed. {reason}", last)
        minutes = (deadline - now).total_seconds() / 60
        if blocking:
            return build(
                SLAState.AT_RISK,
                f"Today's refresh is blocked by a failure; {minutes:.0f} minutes until the {deadline:%H:%M %Z} "
                "deadline",
                last,
            )
        return build(SLAState.ON_TRACK, f"Awaiting today's refresh; {minutes:.0f} minutes to deadline", last)
