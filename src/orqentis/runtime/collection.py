"""Collect telemetry and run detection as one serialised unit of work.

The scheduler, ``POST /telemetry/collect`` and the demo all call :meth:`CollectionService.run`. The lock makes them
take turns, so two collections never race on the same watermarks or open the same incident twice.

Detection does not depend on this run having seen its own events: it reads the event store from a durable cursor
(:class:`~orqentis.incidents.detection_runner.DetectionRunner`), so events stored by an interrupted run are detected by
the interrupted run's cleanup or, failing that, by the next one.

Every run ends in one ``orqentis.collection.runs`` outcome, and the heartbeat (the time of the last successful
collection) moves only for a run that finished, ran detection and did not fail outright:

* ``success``: every collector and scope worked;
* ``degraded``: some failed, at least one worked (FabricOps is partly blind; ``collector.runs{outcome=error}`` alerts);
* ``failed``: everything that was tried failed, or the run raised (an expired credential, a Fabric outage, a bug);
* ``cancelled``: the run was cut short (deploy, lost lease, job timeout).
"""

from __future__ import annotations

import asyncio

from orqentis.config.clock import Clock, SystemClock
from orqentis.domain.incidents import Incident
from orqentis.incidents.detection_runner import DetectionRunner
from orqentis.observability.logging import get_logger
from orqentis.observability.metrics import DISABLED, Metrics
from orqentis.telemetry.collectors import IngestionResult, TelemetryIngestor

_log = get_logger(__name__)


class CollectionService:
    def __init__(
        self,
        ingestor: TelemetryIngestor,
        detection: DetectionRunner,
        *,
        metrics: Metrics = DISABLED,
        clock: Clock | None = None,
    ) -> None:
        self._ingestor = ingestor
        self._detection = detection
        self._metrics = metrics
        self._clock = clock or SystemClock()
        self._lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self._lock.locked()

    async def run(self) -> tuple[IngestionResult, list[Incident]]:
        async with self._lock:
            try:
                ingestion = await self._ingestor.run()
            except BaseException as exc:
                # Cancelled (deploy, lost lease, job timeout) or a collector blew up: whatever was already stored
                # still has to reach detection. If even this is cut short, the cursor makes the next run do it.
                self._metrics.collection_run("cancelled" if isinstance(exc, asyncio.CancelledError) else "failed")
                try:
                    await self._detection.run()
                except Exception:
                    _log.warning(
                        "detection after an interrupted collection failed; the next run retries", exc_info=True
                    )
                raise
            try:
                created = await self._detection.run()
            except BaseException as exc:
                self._metrics.collection_run("cancelled" if isinstance(exc, asyncio.CancelledError) else "failed")
                raise
            self._metrics.collection_run(self._outcome(ingestion), at=self._clock.now())
            return ingestion, created

    @staticmethod
    def _outcome(ingestion: IngestionResult) -> str:
        if ingestion.units_failed == 0:
            return "success"
        return "failed" if ingestion.units_ok == 0 else "degraded"
