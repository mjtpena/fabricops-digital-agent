"""Collect telemetry and run detection as one serialised unit of work.

The scheduler, ``POST /telemetry/collect`` and the demo all call :meth:`CollectionService.run`. The lock makes them
take turns, so two collections never race on the same watermarks or open the same incident twice.

Detection does not depend on this run having seen its own events: it reads the event store from a durable cursor
(:class:`~orqentis.incidents.detection_runner.DetectionRunner`), so events stored by an interrupted run are detected by
the interrupted run's cleanup or, failing that, by the next one.
"""

from __future__ import annotations

import asyncio

from orqentis.domain.incidents import Incident
from orqentis.incidents.detection_runner import DetectionRunner
from orqentis.observability.logging import get_logger
from orqentis.telemetry.collectors import IngestionResult, TelemetryIngestor

_log = get_logger(__name__)


class CollectionService:
    def __init__(self, ingestor: TelemetryIngestor, detection: DetectionRunner) -> None:
        self._ingestor = ingestor
        self._detection = detection
        self._lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self._lock.locked()

    async def run(self) -> tuple[IngestionResult, list[Incident]]:
        async with self._lock:
            try:
                ingestion = await self._ingestor.run()
            except BaseException:
                # Cancelled (deploy, lost lease, job timeout) or a collector blew up: whatever was already stored
                # still has to reach detection. If even this is cut short, the cursor makes the next run do it.
                try:
                    await self._detection.run()
                except Exception:
                    _log.warning(
                        "detection after an interrupted collection failed; the next run retries", exc_info=True
                    )
                raise
            return ingestion, await self._detection.run()
