"""Collectors: deterministic software that observes the estate (the LLM never monitors)."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol, runtime_checkable

from orqentis.config.clock import Clock
from orqentis.domain.common import ItemType
from orqentis.domain.events import OperationalEvent
from orqentis.domain.runs import RunStatus
from orqentis.fabric.errors import FabricError
from orqentis.fabric.ports import FabricClient
from orqentis.observability.logging import get_logger
from orqentis.observability.metrics import DISABLED, Metrics
from orqentis.storage.event_store import COLLECTOR_SCOPE, EventStore
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


@runtime_checkable
class ScopedCollector(Protocol):
    """A collector that observes many independent things (pipelines, semantic models, capacities).

    The ingestor keeps one watermark **per scope** and gives each scope its own window. A scope that appears later
    (a pipeline created after the last catalog refresh) has no watermark, so it is read from the maximum lookback
    instead of inheriting the collector's progress; one failing scope does not hold back the others.
    """

    name: str

    def scopes(self) -> list[str]:
        """Ids to collect right now (read from the live catalog)."""
        ...

    async def collect_scope(self, scope: str, *, since: datetime, until: datetime) -> list[OperationalEvent]: ...


class PipelineRunCollector:
    name = "pipeline-runs"

    def __init__(self, fabric: FabricClient, catalog: EstateCatalog) -> None:
        self._fabric = fabric
        self._catalog = catalog

    def scopes(self) -> list[str]:
        return [i.id for i in self._catalog.items.values() if i.item_type is ItemType.DATA_PIPELINE]

    async def collect_scope(self, scope: str, *, since: datetime, until: datetime) -> list[OperationalEvent]:
        item = self._catalog.items.get(scope)
        if item is None or item.item_type is not ItemType.DATA_PIPELINE:
            return []  # removed from the estate since the scopes were listed
        events: list[OperationalEvent] = []
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

    async def collect(self, *, since: datetime, until: datetime) -> list[OperationalEvent]:
        events: list[OperationalEvent] = []
        for scope in self.scopes():
            events.extend(await self.collect_scope(scope, since=since, until=until))
        return events


class RefreshCollector:
    name = "semantic-model-refreshes"

    def __init__(self, fabric: FabricClient, catalog: EstateCatalog) -> None:
        self._fabric = fabric
        self._catalog = catalog

    def scopes(self) -> list[str]:
        return [i.id for i in self._catalog.items.values() if i.item_type is ItemType.SEMANTIC_MODEL]

    async def collect_scope(self, scope: str, *, since: datetime, until: datetime) -> list[OperationalEvent]:
        item = self._catalog.items.get(scope)
        if item is None or item.item_type is not ItemType.SEMANTIC_MODEL:
            return []
        events: list[OperationalEvent] = []
        for refresh in await self._fabric.semantic_models.get_refresh_history(item.workspace_id, item.id, top=20):
            when = refresh.end_time or refresh.start_time
            if is_reportable(refresh.status) and when is not None and since <= when <= until:
                events.append(refresh_event(refresh, display_name=item.display_name, environment=item.environment))
        return events

    async def collect(self, *, since: datetime, until: datetime) -> list[OperationalEvent]:
        events: list[OperationalEvent] = []
        for scope in self.scopes():
            events.extend(await self.collect_scope(scope, since=since, until=until))
        return events


class CapacityCollector:
    name = "capacity-metrics"

    def __init__(self, fabric: FabricClient, catalog: EstateCatalog) -> None:
        self._fabric = fabric
        self._catalog = catalog

    def scopes(self) -> list[str]:
        return list(self._catalog.capacities)

    async def collect_scope(self, scope: str, *, since: datetime, until: datetime) -> list[OperationalEvent]:
        capacity = self._catalog.capacities.get(scope)
        if capacity is None:
            return []
        events: list[OperationalEvent] = []
        for snapshot in await self._fabric.capacities.get_capacity_metrics(capacity.id, start=since, end=until):
            events.extend(capacity_events(snapshot, capacity))
        return events

    async def collect(self, *, since: datetime, until: datetime) -> list[OperationalEvent]:
        events: list[OperationalEvent] = []
        for scope in self.scopes():
            events.extend(await self.collect_scope(scope, since=since, until=until))
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
    # Units of work: a whole collector, or one scope of a scoped collector. ``Metrics`` and ``CollectionService`` use
    # the split to tell "some failed" (degraded) from "everything failed" (blind).
    units_ok: int = 0
    units_failed: int = 0
    # The (since, until) window each collector (or, for scoped collectors, each ``collector:scope``) was asked for;
    # ones that failed are listed too.
    windows: dict[str, tuple[datetime, datetime]] = field(default_factory=dict)

    def counts(self) -> dict[str, int]:
        return dict(Counter(e.event_type.value for e in self.new_events))


class TelemetryIngestor:
    """Runs each collector over its own window and appends normalised events to the event store.

    The window of a collector (or of one scope of a scoped collector) is ``[watermark - overlap, now]``: the
    watermark is the instant up to which a previous run completed, and the overlap re-reads a little history so
    late-arriving data is not missed (the event store de-duplicates by ``dedupe_key``, so re-reading is harmless).
    The window never reaches further back than ``lookback``, which is also the window of anything that has never
    been collected. A watermark advances only after its events were stored, so a failure is retried over the same
    window. A watermark that is *ahead* of the clock (the clock was set back) is not trusted: the maximum lookback
    is re-read once and the watermark is corrected.

    Storing events is not the end of the pipeline: detection consumes them from the event store through a durable
    cursor (see :class:`~orqentis.incidents.detection_runner.DetectionRunner`), so what this class appended before
    being interrupted is never lost to detection.
    """

    def __init__(
        self,
        collectors: Sequence[Collector],
        event_store: EventStore,
        clock: Clock,
        *,
        lookback: timedelta = timedelta(days=7),
        overlap: timedelta = timedelta(minutes=15),
        metrics: Metrics = DISABLED,
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
        self._metrics = metrics

    def _window_start(self, watermark: datetime | None, until: datetime, label: str) -> tuple[datetime, bool]:
        """``(since, clock_repair_needed)`` for one watermark."""
        floor = until - self._lookback
        if watermark is None:
            return floor, False
        if watermark > until:
            _log.warning(
                "collector watermark is ahead of the clock; re-reading the maximum lookback and correcting it",
                extra={"collector": label, "watermark": watermark.isoformat(), "now": until.isoformat()},
            )
            return floor, True
        return max(watermark - self._overlap, floor), False

    async def run(self) -> IngestionResult:
        until = self._clock.now()
        result = IngestionResult()
        for collector in self._collectors:
            if isinstance(collector, ScopedCollector):
                await self._run_scoped(collector, until, result)
            else:
                await self._run_whole(collector, until, result)
        result.new_events.sort(key=lambda e: e.timestamp)
        _log.info(
            "telemetry ingested",
            extra={"collected": result.collected, "new": len(result.new_events), "by_type": result.counts()},
        )
        return result

    async def _store(self, events: list[OperationalEvent], result: IngestionResult) -> int:
        """Append the events; returns how many were new to the event store."""
        result.collected += len(events)
        events.sort(key=lambda e: e.timestamp)
        appended = await self._event_store.append(events)
        result.new_events.extend(appended)
        return len(appended)

    def _seconds_since(self, started: datetime) -> float:
        return (self._clock.now() - started).total_seconds()

    async def _run_whole(self, collector: Collector, until: datetime, result: IngestionResult) -> None:
        started = self._clock.now()
        watermarks = await self._event_store.get_watermarks(collector.name)
        since, repair = self._window_start(watermarks.get(COLLECTOR_SCOPE), until, collector.name)
        result.windows[collector.name] = (since, until)
        try:
            events = await collector.collect(since=since, until=until)
        except FabricError as exc:
            # One failing collector must not blind the others, and its watermark stays where it was.
            result.errors[collector.name] = exc.message
            result.units_failed += 1
            self._metrics.collector_run(collector.name, seconds=self._seconds_since(started), errors=1, new_events=0)
            _log.warning("collector failed", extra={"collector": collector.name, "error": exc.to_dict()})
            return
        except Exception:
            self._metrics.collector_run(collector.name, seconds=self._seconds_since(started), errors=1, new_events=0)
            raise
        new_events = await self._store(events, result)
        await self._event_store.advance_watermarks(collector.name, {COLLECTOR_SCOPE: until}, force=repair)
        result.units_ok += 1
        self._metrics.collector_run(
            collector.name, seconds=self._seconds_since(started), errors=0, new_events=new_events
        )

    async def _run_scoped(self, collector: ScopedCollector, until: datetime, result: IngestionResult) -> None:
        started = self._clock.now()
        watermarks = await self._event_store.get_watermarks(collector.name)
        done: dict[str, datetime] = {}
        repaired: dict[str, datetime] = {}
        failed_scopes = 0
        new_events = 0
        try:
            for scope in collector.scopes():
                label = f"{collector.name}:{scope}"
                since, repair = self._window_start(watermarks.get(scope), until, label)
                result.windows[label] = (since, until)
                try:
                    events = await collector.collect_scope(scope, since=since, until=until)
                except FabricError as exc:
                    result.errors[label] = exc.message
                    result.units_failed += 1
                    failed_scopes += 1
                    _log.warning("collector failed", extra={"collector": label, "error": exc.to_dict()})
                    continue
                new_events += await self._store(events, result)
                result.units_ok += 1
                (repaired if repair else done)[scope] = until
        except Exception:
            self._metrics.collector_run(
                collector.name, seconds=self._seconds_since(started), errors=failed_scopes + 1, new_events=new_events
            )
            raise
        finally:
            # Also when this run is cut short: what was stored is recorded, so the next run does not redo it.
            await self._event_store.advance_watermarks(collector.name, done)
            await self._event_store.advance_watermarks(collector.name, repaired, force=True)
        self._metrics.collector_run(
            collector.name, seconds=self._seconds_since(started), errors=failed_scopes, new_events=new_events
        )
