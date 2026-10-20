"""Durable detection: every stored event reaches the detection rules, even across interruptions.

Detection used to run on the events that one collection run had just appended. If that run was cancelled (a deploy, a
lost lease, the job timeout) or a later collector failed, the events it had already stored were de-duplicated as "not
new" on the next run and detection never saw them. :class:`DetectionRunner` instead consumes the event store through a
**cursor**: the event store numbers every inserted event, the runner reads the events after its cursor, runs the
detection engine on them, and moves the cursor only once the engine returned (so the incidents are persisted).
Anything interrupted is simply read again; the engine is idempotent (it de-duplicates by item and correlation, or by
the triggering event), so re-reading is harmless.
"""

from __future__ import annotations

from orqentis.domain.incidents import Incident
from orqentis.incidents.detection import DetectionEngine
from orqentis.observability.logging import get_logger
from orqentis.runtime.mutex import InProcessMutex, Mutex
from orqentis.storage.event_store import EventStore

_log = get_logger(__name__)

DETECTION_CONSUMER = "detection"


class DetectionRunner:
    def __init__(
        self,
        event_store: EventStore,
        engine: DetectionEngine,
        *,
        consumer: str = DETECTION_CONSUMER,
        batch_size: int = 500,
        mutex: Mutex | None = None,
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        self._events = event_store
        self._engine = engine
        self._consumer = consumer
        self._batch_size = batch_size
        # Detection is check-then-act (is there an incident for this failure? open one). Across replicas the mutex
        # keeps two passes (the leader's scheduled run, a manual collection elsewhere) from both opening it.
        self._mutex: Mutex = mutex if mutex is not None else InProcessMutex()

    async def run(self) -> list[Incident]:
        """Process every event stored since the last run; returns the incidents that were opened."""
        async with self._mutex.hold("detection"):
            created: list[Incident] = []
            position = await self._events.get_cursor(self._consumer)
            processed = 0
            while batch := await self._events.read_after(position, limit=self._batch_size):
                created.extend(await self._engine.process([item.event for item in batch]))
                position = await self._events.advance_cursor(self._consumer, batch[-1].sequence)
                processed += len(batch)
            if processed:
                _log.info(
                    "detection pass",
                    extra={"events": processed, "incidents_created": [i.incident_id for i in created]},
                )
            return created
