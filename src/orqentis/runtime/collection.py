"""Collect telemetry and run detection as one serialised unit of work.

The scheduler, ``POST /telemetry/collect`` and the demo all call :meth:`CollectionService.run`. The lock makes them
take turns, so two collections never race on the same watermarks or open the same incident twice.
"""

from __future__ import annotations

import asyncio

from orqentis.domain.incidents import Incident
from orqentis.incidents.detection import DetectionEngine
from orqentis.telemetry.collectors import IngestionResult, TelemetryIngestor


class CollectionService:
    def __init__(self, ingestor: TelemetryIngestor, detection: DetectionEngine) -> None:
        self._ingestor = ingestor
        self._detection = detection
        self._lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self._lock.locked()

    async def run(self) -> tuple[IngestionResult, list[Incident]]:
        async with self._lock:
            ingestion = await self._ingestor.run()
            created = await self._detection.process(ingestion.new_events)
            return ingestion, created
