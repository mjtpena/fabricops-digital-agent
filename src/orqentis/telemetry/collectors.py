"""Collectors: deterministic software that observes the estate (the LLM never monitors)."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

from orqentis.config.clock import Clock
from orqentis.domain.common import ItemType
from orqentis.domain.events import OperationalEvent
from orqentis.domain.runs import RunStatus
from orqentis.fabric.errors import FabricError
from orqentis.fabric.ports import FabricClient
from orqentis.observability.logging import get_logger
from orqentis.storage.event_store import EventStore
from orqentis.telemetry.normalization import (
    activity_failure_event,
    capacity_events,
    is_reportable,
    pipeline_run_event,
    refresh_event,
)
from orqentis.topology.catalog import EstateCatalog

_log = get_logger(__name__)

# Fabric keeps about the 100 most recent completed job instances per item (plus every active one).
_JOB_INSTANCE_LIMIT = 100


class Collector(Protocol):
    name: str

    async def collect(self, *, since: datetime, until: datetime) -> list[OperationalEvent]: ...


class PipelineRunCollector:
    name = "pipeline-runs"

    def __init__(self, fabric: FabricClient, catalog: EstateCatalog) -> None:
        self._fabric = fabric
        self._catalog = catalog

    async def collect(self, *, since: datetime, until: datetime) -> list[OperationalEvent]:
        events: list[OperationalEvent] = []
        for item in self._catalog.items.values():
            if item.item_type is not ItemType.DATA_PIPELINE:
                continue
            runs = await self._fabric.jobs.list_job_instances(
                item.workspace_id, item.id, limit=_JOB_INSTANCE_LIMIT, since=since
            )
            for run in runs:
                when = run.end_time or run.start_time
                if not is_reportable(run.status) or when is None or not since <= when <= until:
                    continue
                events.append(pipeline_run_event(run, display_name=item.display_name, environment=item.environment))
                if run.status is RunStatus.FAILED:
                    activities = await self._fabric.pipelines.query_activity_runs(item.workspace_id, item.id, run.id)
                    events.extend(
                        activity_failure_event(a, pipeline_name=item.display_name, environment=item.environment)
                        for a in activities
                        if a.status.is_failure
                    )
        return events


class RefreshCollector:
    name = "semantic-model-refreshes"

    def __init__(self, fabric: FabricClient, catalog: EstateCatalog) -> None:
        self._fabric = fabric
        self._catalog = catalog

    async def collect(self, *, since: datetime, until: datetime) -> list[OperationalEvent]:
        events: list[OperationalEvent] = []
        for item in self._catalog.items.values():
            if item.item_type is not ItemType.SEMANTIC_MODEL:
                continue
            for refresh in await self._fabric.semantic_models.get_refresh_history(item.workspace_id, item.id, top=20):
                when = refresh.end_time or refresh.start_time
                if is_reportable(refresh.status) and when is not None and since <= when <= until:
                    events.append(refresh_event(refresh, display_name=item.display_name, environment=item.environment))
        return events


class CapacityCollector:
    name = "capacity-metrics"

    def __init__(self, fabric: FabricClient, catalog: EstateCatalog) -> None:
        self._fabric = fabric
        self._catalog = catalog

    async def collect(self, *, since: datetime, until: datetime) -> list[OperationalEvent]:
        events: list[OperationalEvent] = []
        for capacity in self._catalog.capacities.values():
            for snapshot in await self._fabric.capacities.get_capacity_metrics(capacity.id, start=since, end=until):
                events.extend(capacity_events(snapshot, capacity))
        return events


class StaticEventCollector:
    """Replays pre-normalised events (e.g. the synthetic Delta-log schema watcher in mock mode)."""

    name = "static-events"

    def __init__(self, events: Sequence[OperationalEvent]) -> None:
        self._events = list(events)

    async def collect(self, *, since: datetime, until: datetime) -> list[OperationalEvent]:
        return [e for e in self._events if since <= e.timestamp <= until]


@dataclass
class IngestionResult:
    new_events: list[OperationalEvent] = field(default_factory=list)
    collected: int = 0
    errors: dict[str, str] = field(default_factory=dict)
    # The (since, until) window each collector was asked for; collectors that failed are listed too.
    windows: dict[str, tuple[datetime, datetime]] = field(default_factory=dict)

    def counts(self) -> dict[str, int]:
        return dict(Counter(e.event_type.value for e in self.new_events))


class TelemetryIngestor:
    """Runs each collector over its own window and appends normalised events to the event store.

    The window of a collector is ``[watermark - overlap, now]``: the watermark is the instant up to which a previous
    run completed, and the overlap re-reads a little history so late-arriving data is not missed (the event store
    de-duplicates by ``dedupe_key``, so re-reading is harmless). The window never reaches further back than
    ``lookback``, which is also the window of a collector that has never run. A collector's watermark advances only
    after its events were stored, so a failed run is retried over the same window.
    """

    def __init__(
        self,
        collectors: Sequence[Collector],
        event_store: EventStore,
        clock: Clock,
        *,
        lookback: timedelta = timedelta(days=7),
        overlap: timedelta = timedelta(minutes=15),
    ) -> None:
        if lookback <= timedelta(0):
            raise ValueError("lookback must be positive")
        if overlap < timedelta(0):
            raise ValueError("overlap must not be negative")
        self._collectors = list(collectors)
        self._event_store = event_store
        self._clock = clock
        self._lookback = lookback
        self._overlap = overlap

    async def window_start(self, collector: Collector, *, until: datetime) -> datetime:
        floor = until - self._lookback
        watermark = await self._event_store.get_watermark(collector.name)
        if watermark is None:
            return floor
        return min(max(watermark - self._overlap, floor), until)

    async def run(self) -> IngestionResult:
        until = self._clock.now()
        result = IngestionResult()
        for collector in self._collectors:
            since = await self.window_start(collector, until=until)
            result.windows[collector.name] = (since, until)
            try:
                events = await collector.collect(since=since, until=until)
            except FabricError as exc:
                # One failing collector must not blind the others, and its watermark stays where it was.
                result.errors[collector.name] = exc.message
                _log.warning("collector failed", extra={"collector": collector.name, "error": exc.to_dict()})
                continue
            result.collected += len(events)
            events.sort(key=lambda e: e.timestamp)
            result.new_events.extend(await self._event_store.append(events))
            await self._event_store.advance_watermark(collector.name, until)
        result.new_events.sort(key=lambda e: e.timestamp)
        _log.info(
            "telemetry ingested",
            extra={"collected": result.collected, "new": len(result.new_events), "by_type": result.counts()},
        )
        return result
