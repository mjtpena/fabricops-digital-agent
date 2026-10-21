"""Collectors: deterministic software that observes the estate (the LLM never monitors)."""

from __future__ import annotations

from collections import Counter
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol, runtime_checkable

from orqentis.config.clock import Clock
from orqentis.domain.common import ItemType
from orqentis.domain.delta import DeltaCommitRef, DeltaTableRef
from orqentis.domain.estate import DeltaTableDeclaration, FabricItem, GenericItem
from orqentis.domain.events import EventType, OperationalEvent
from orqentis.domain.runs import RunStatus
from orqentis.fabric.errors import FabricAuthError, FabricCapabilityUnavailableError, FabricError
from orqentis.fabric.ports import FabricClient
from orqentis.observability.logging import get_logger
from orqentis.observability.metrics import DISABLED, Metrics
from orqentis.storage.event_store import COLLECTOR_SCOPE, EventQuery, EventStore
from orqentis.telemetry.delta import ParsedCommit, SchemaSnapshot, parse_commit, schema_change
from orqentis.telemetry.normalization import (
    JOB_EVENTS,
    activity_failure_event,
    capacity_events,
    is_reportable,
    item_job_event,
    pipeline_run_event,
    refresh_event,
    schema_change_event,
    schema_snapshot_event,
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


@runtime_checkable
class ResumableCollector(ScopedCollector, Protocol):
    """A scoped collector that may stop before the end of a scope's window and say how far it got.

    Without it a collector that caps its work per run would either drop what it did not reach (the watermark passes it)
    or never get past it (the watermark stays and the same part is read again).
    """

    def progress(self, scope: str) -> datetime | None:
        """After ``collect_scope(scope)``: the instant everything before which was processed, ``None`` if all of it."""
        ...


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


class ItemJobCollector:
    """Job instances of notebooks, Spark job definitions or Dataflows (one collector per item type, ADR-0020).

    Same shape as :class:`PipelineRunCollector` without the activity runs: per-item watermarks, ``since`` filtering
    after reading, and only finished runs.
    """

    COLLECTOR_NAMES = {
        ItemType.NOTEBOOK: "notebook-runs",
        ItemType.SPARK_JOB_DEFINITION: "spark-job-runs",
        ItemType.DATAFLOW: "dataflow-runs",
    }

    def __init__(self, fabric: FabricClient, catalog: EstateCatalog, item_type: ItemType) -> None:
        if item_type not in JOB_EVENTS:
            raise ValueError(f"no job collector for {item_type.value} items")
        self._fabric = fabric
        self._catalog = catalog
        self._item_type = item_type
        self.name = self.COLLECTOR_NAMES[item_type]

    def scopes(self) -> list[str]:
        return [i.id for i in self._catalog.items.values() if i.item_type is self._item_type]

    async def collect_scope(self, scope: str, *, since: datetime, until: datetime) -> list[OperationalEvent]:
        item = self._catalog.items.get(scope)
        if item is None or item.item_type is not self._item_type:
            return []  # removed from the estate since the scopes were listed
        runs = await self._fabric.jobs.list_job_instances(
            item.workspace_id, item.id, limit=_JOB_INSTANCE_LIMIT, since=since
        )
        events: list[OperationalEvent] = []
        for run in runs:
            when = run.end_time or run.start_time
            if not is_reportable(run.status) or when is None or not since <= when <= until:
                continue
            events.append(
                item_job_event(
                    run, item_type=self._item_type, display_name=item.display_name, environment=item.environment
                )
            )
        return events

    async def collect(self, *, since: datetime, until: datetime) -> list[OperationalEvent]:
        events: list[OperationalEvent] = []
        for scope in self.scopes():
            events.extend(await self.collect_scope(scope, since=since, until=until))
        return events


class MonitoringRefreshCollector:
    """Semantic model refresh outcomes from Fabric workspace monitoring (decision D9, ADR-0020).

    One scope per workspace that holds a semantic model. Where the catalog has no monitoring database for that
    workspace the scope is reported as *capability unavailable* without any API call: the refresh failures of those
    models cannot be seen, and that must be visible (metric, readiness) instead of looking like a quiet workspace
    (docs/spikes/workspace-monitoring.md, section 10). A monitoring database that exists but cannot be read is the
    adapter's to classify; an ambiguous one is an error.

    The rows a monitoring database returns are untrusted: anyone who can run a refresh in the workspace writes them. A
    row is used only when its item is a semantic model of *this* workspace in the catalog, so a database cannot name a
    model of another workspace (a PROD model, say) and have an incident opened against it.
    """

    name = "workspace-monitoring-refreshes"
    # Events reach the monitoring database minutes after they happen (documentation: 10 to 15 minutes at worst), so a
    # refresh that just ended can still look unfinished. Reading this much further back than the window means the End
    # row is found on a later run; the event store de-duplicates by refresh id and status.
    LATENCY_MARGIN = timedelta(minutes=30)

    def __init__(self, fabric: FabricClient, catalog: EstateCatalog, *, database_name: str) -> None:
        self._fabric = fabric
        self._catalog = catalog
        self._database_name = database_name.strip().lower()

    def scopes(self) -> list[str]:
        return sorted({i.workspace_id for i in self._catalog.items.values() if i.item_type is ItemType.SEMANTIC_MODEL})

    def _has_monitoring(self, workspace_id: str) -> bool:
        return any(
            isinstance(i, GenericItem)
            and i.workspace_id == workspace_id
            and i.fabric_type == "KQLDatabase"
            and i.display_name.strip().lower() == self._database_name
            for i in self._catalog.items.values()
        )

    async def collect_scope(self, scope: str, *, since: datetime, until: datetime) -> list[OperationalEvent]:
        if not self._has_monitoring(scope):
            raise FabricCapabilityUnavailableError(
                f"Semantic model refreshes of workspace {scope} cannot be observed: the estate has no KQL database "
                "named like the workspace monitoring database there (monitoring is not enabled, or the reader cannot "
                "see it)",
                error_code="WorkspaceMonitoringNotEnabled",
                details={"workspace_id": scope},
            )
        models = {
            i.id: i
            for i in self._catalog.items.values()
            if i.item_type is ItemType.SEMANTIC_MODEL and i.workspace_id == scope
        }
        events: list[OperationalEvent] = []
        foreign = 0
        window_start = since - self.LATENCY_MARGIN
        for refresh in await self._fabric.monitoring.list_semantic_model_refreshes(
            scope, since=window_start, until=until
        ):
            when = refresh.end_time or refresh.start_time
            if not is_reportable(refresh.status) or when is None or not window_start <= when <= until:
                continue
            model = models.get(refresh.item_id)
            if model is None:
                foreign += 1
                continue
            events.append(refresh_event(refresh, display_name=model.display_name, environment=model.environment))
        if foreign:
            # A model created since the catalog was built, or a row that names an item that is not ours. Counted, not
            # named: the id came from the database.
            _log.warning(
                "workspace monitoring rows skipped: not a semantic model of this workspace in the estate",
                extra={"workspace_id": scope, "skipped": foreign},
            )
        return events

    async def collect(self, *, since: datetime, until: datetime) -> list[OperationalEvent]:
        events: list[OperationalEvent] = []
        for scope in self.scopes():
            events.extend(await self.collect_scope(scope, since=since, until=until))
        return events


class SchemaChangeCollector:
    """Schema changes of the Delta tables declared in the contracts, read from their ``_delta_log`` (ADR-0009).

    One scope per declared table. A scope reads the JSON commits it has not processed yet, oldest first, and compares
    each schema it finds with the one before it. Only a difference in the columns or their types produces a
    ``SCHEMA_CHANGED`` event: re-written but identical schemas (a table property changed, an overwrite with the same
    columns) produce nothing.

    **What "before" is** is remembered, not searched for: whenever a commit brings a schema that differs from the one in
    force, the collector also returns a ``SCHEMA_SNAPSHOT`` event holding it, and the next run starts from the newest
    snapshot of the table. Delta writes a ``metaData`` action only when the schema changes, so on a busy table the
    commit that carries the previous schema can be thousands of commits back; a bounded search through the log would
    miss exactly the tables that matter. The search (``max_backscan`` commits) is only the way a table is first seen.

    **Work per run is bounded without losing anything.** At most ``max_commits`` commits are read per scope per run,
    the *oldest* unprocessed ones. When more are waiting the collector says so (:meth:`progress`), the ingestor moves
    the watermark only as far as the last commit processed, and a checkpoint snapshot records the version reached, so
    the next run continues after it instead of reading the same commits again.
    """

    name = "delta-schema-changes"
    SOURCE = "orqentis.collectors.delta-log"

    def __init__(
        self,
        fabric: FabricClient,
        catalog: EstateCatalog,
        event_store: EventStore,
        *,
        max_commits: int = 50,
        max_backscan: int = 20,
    ) -> None:
        if max_commits < 1 or max_backscan < 0:
            raise ValueError("max_commits must be >= 1 and max_backscan >= 0")
        self._fabric = fabric
        self._catalog = catalog
        self._event_store = event_store
        self._max_commits = max_commits
        self._max_backscan = max_backscan
        self._resume: dict[str, datetime] = {}

    def scopes(self) -> list[str]:
        return sorted(
            t.id
            for t in self._catalog.delta_tables.values()
            if (item := self._catalog.items.get(t.item_id)) is not None and item.item_type is ItemType.LAKEHOUSE
        )

    def progress(self, scope: str) -> datetime | None:
        """Set by the last :meth:`collect_scope` of ``scope`` when commits were left for the next run.

        The instant up to which the commits were processed (the watermark may move that far, no further), or ``None``
        when the whole window was read. Read once: the value is consumed.
        """
        return self._resume.pop(scope, None)

    async def collect_scope(self, scope: str, *, since: datetime, until: datetime) -> list[OperationalEvent]:
        self._resume.pop(scope, None)
        declaration = self._catalog.delta_tables.get(scope)
        lakehouse = self._catalog.items.get(declaration.item_id) if declaration else None
        if declaration is None or lakehouse is None:
            return []
        table = DeltaTableRef(
            id=declaration.id,
            item_id=declaration.item_id,
            workspace_id=lakehouse.workspace_id,
            table=declaration.table,
            schema_name=declaration.schema_name,
        )
        try:
            return await self._read_table(declaration, lakehouse, table, since=since, until=until)
        except FabricError:
            raise
        except Exception as exc:
            # The log is written by whoever can write to the table: whatever it contains must cost this table its run,
            # never the other tables' (or the other collectors') runs. Only the exception's type is kept: its message
            # may quote what was read.
            raise FabricError(
                f"The Delta log of {declaration.id} could not be processed ({type(exc).__name__})",
                error_code="DeltaLogUnreadable",
            ) from exc

    async def _read_table(
        self,
        declaration: DeltaTableDeclaration,
        lakehouse: FabricItem,
        table: DeltaTableRef,
        *,
        since: datetime,
        until: datetime,
    ) -> list[OperationalEvent]:
        commits = await self._fabric.delta_logs.list_commits(table)
        stored = await self._stored_baseline(declaration.id)
        floor = stored.version if stored else -1
        if stored and commits and commits[-1].version < stored.version:
            floor = -1  # the log starts over (the table was dropped and created again): everything in it is new
        pending = [c for c in commits if c.version > floor and (c.modified_at is None or c.modified_at >= since)]
        batch = pending[: self._max_commits]

        def event_time(ref: DeltaCommitRef | None, parsed: ParsedCommit | None = None) -> datetime:
            when = (parsed.committed_at if parsed else None) or (ref.modified_at if ref else None) or until
            return min(when, until)

        def snapshot(version: int, schema: SchemaSnapshot | None, *, checkpoint: bool = False) -> OperationalEvent:
            # Stamped with the collection time, not the commit's: the order of snapshots is the order they were taken in
            # (the writer of the log chooses the commit's own timestamp).
            return schema_snapshot_event(
                declaration=declaration,
                version=version,
                schema=schema,
                tenant_id=lakehouse.tenant_id,
                workspace_id=lakehouse.workspace_id,
                lakehouse_name=lakehouse.display_name,
                environment=lakehouse.environment,
                timestamp=until,
                source=self.SOURCE,
                checkpoint=checkpoint,
            )

        events: list[OperationalEvent] = []
        previous: tuple[int, SchemaSnapshot] | None = (
            (stored.version, stored.schema) if stored and stored.schema else None
        )
        if previous is None and stored is None:
            # A table seen for the first time: the newest schema before what is read now is the starting point.
            earlier = [c for c in commits if not batch or c.version < batch[0].version]
            found = await self._first_schema(table, earlier)
            if found is not None and found[1].schema is not None:
                previous = (found[0].version, found[1].schema)
                events.append(snapshot(found[0].version, found[1].schema))
        last: DeltaCommitRef | None = None
        for ref in batch:
            parsed = parse_commit(await self._fabric.delta_logs.read_commit(table, ref.version))
            last = ref
            if parsed.schema is None:
                continue
            if previous is not None and previous[1].same_as(parsed.schema):
                continue  # the same columns re-written
            if previous is not None:
                change = schema_change(declaration.table, previous, (ref.version, parsed.schema), parsed)
                if change is not None:
                    events.append(
                        schema_change_event(
                            change,
                            declaration=declaration,
                            tenant_id=lakehouse.tenant_id,
                            workspace_id=lakehouse.workspace_id,
                            lakehouse_name=lakehouse.display_name,
                            environment=lakehouse.environment,
                            timestamp=event_time(ref, parsed),
                            source=self.SOURCE,
                            user_metadata=parsed.user_metadata,
                            truncated=parsed.truncated,
                        )
                    )
            events.append(snapshot(ref.version, parsed.schema))
            previous = (ref.version, parsed.schema)
        partial = last is not None and len(pending) > len(batch)
        reached = last.version if last is not None else (commits[-1].version if commits else None)
        # A snapshot of the version reached already says how far the run got.
        recorded = (
            bool(events) and events[-1].event_type is EventType.SCHEMA_SNAPSHOT and _version_of(events[-1]) == reached
        )
        if reached is not None and not recorded and (partial or (stored is None and previous is None)):
            # Record how far this run got: when commits are left, so the next run resumes after this one; when the table
            # was searched for a schema and none was found, so it is not searched again on every run.
            events.append(snapshot(reached, previous[1] if previous else None, checkpoint=True))
        if partial and last is not None:
            self._resume[declaration.id] = last.modified_at or since
        return events

    async def _stored_baseline(self, table_id: str) -> _Baseline | None:
        """The newest snapshot recorded for the table: the schema in force at the last commit processed."""
        found = await self._event_store.query(
            EventQuery(event_types=[EventType.SCHEMA_SNAPSHOT], correlation_id=table_id, limit=50, newest_first=True)
        )
        # The snapshots of one run share a timestamp: among the newest, the highest version is the last processed.
        # (After a log restart the new snapshots are newer, so they win.)
        recorded = [(e.timestamp, v, e) for e in found if isinstance(v := e.payload.get("version"), int) and v >= 0]
        if not recorded:
            return None
        _, version, newest = max(recorded, key=lambda r: (r[0], r[1]))
        return _Baseline(version, SchemaSnapshot.from_payload(newest.payload))

    async def _first_schema(
        self, table: DeltaTableRef, earlier: Sequence[DeltaCommitRef]
    ) -> tuple[DeltaCommitRef, ParsedCommit] | None:
        """The newest of the last ``max_backscan`` earlier commits that carries a schema (a table's first sight)."""
        candidates = earlier[-self._max_backscan :] if self._max_backscan else []
        for ref in reversed(candidates):
            parsed: ParsedCommit = parse_commit(await self._fabric.delta_logs.read_commit(table, ref.version))
            if parsed.schema is not None:
                return ref, parsed
        return None

    async def collect(self, *, since: datetime, until: datetime) -> list[OperationalEvent]:
        events: list[OperationalEvent] = []
        for scope in self.scopes():
            events.extend(await self.collect_scope(scope, since=since, until=until))
        return events


@dataclass(frozen=True)
class _Baseline:
    version: int
    schema: SchemaSnapshot | None  # None: a checkpoint of a table whose schema was never seen


def _version_of(event: OperationalEvent) -> object:
    return event.payload.get("version")


# The Capacity Metrics model has 30 second timepoints; a day of 10 minute buckets is plenty of context and bounded.
_CAPACITY_MAX_LOOKBACK = timedelta(hours=24)


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
        start = max(since, until - _CAPACITY_MAX_LOOKBACK)
        for snapshot in await self._fabric.capacities.get_capacity_metrics(capacity.id, start=start, end=until):
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
    # Collectors (or scopes) that cannot observe what they are meant to because the deployment does not give them the
    # means, by design: a documented, configured permission gap, or a capability the platform does not expose. Not a
    # failure: nothing is retried differently and nothing alerts, but it is visible (metric, readiness, API).
    unavailable: dict[str, str] = field(default_factory=dict)
    # Units of work: a whole collector, or one scope of a scoped collector. ``Metrics`` and ``CollectionService`` use
    # the split to tell "some failed" (degraded) from "everything failed" (blind).
    units_ok: int = 0
    units_failed: int = 0
    units_unavailable: int = 0
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
        expected_forbidden: Collection[str] = (),
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
        # Collectors whose HTTP 403 the deployment expects and documents (ORQENTIS_EXPECTED_FORBIDDEN_COLLECTORS):
        # for these a 403 is "capability unavailable". Any other 401/403 stays an error.
        self._expected_forbidden = frozenset(expected_forbidden)

    def _unavailable_reason(self, collector: str, exc: FabricError) -> str | None:
        """Why this failure is a known capability gap rather than a fault, or ``None`` when it is a fault."""
        if isinstance(exc, FabricCapabilityUnavailableError):
            return exc.message
        if isinstance(exc, FabricAuthError) and exc.status_code == 403 and collector in self._expected_forbidden:
            return f"forbidden (HTTP 403), which this deployment expects for {collector}: {exc.message}"
        return None

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

    def _note_unavailable(self, result: IngestionResult, collector: str, label: str, reason: str) -> None:
        result.unavailable[label] = reason
        result.units_unavailable += 1
        self._metrics.collector_unavailable(collector)
        _log.info("collector capability unavailable", extra={"collector": label, "reason": reason})

    async def _run_whole(self, collector: Collector, until: datetime, result: IngestionResult) -> None:
        started = self._clock.now()
        watermarks = await self._event_store.get_watermarks(collector.name)
        since, repair = self._window_start(watermarks.get(COLLECTOR_SCOPE), until, collector.name)
        result.windows[collector.name] = (since, until)
        try:
            events = await collector.collect(since=since, until=until)
        except FabricError as exc:
            if (reason := self._unavailable_reason(collector.name, exc)) is not None:
                self._note_unavailable(result, collector.name, collector.name, reason)
                # Still a run, and its duration still counts: only `outcome=error` feeds the collector-errors alert.
                self._metrics.collector_run(
                    collector.name, seconds=self._seconds_since(started), errors=0, new_events=0, unavailable=True
                )
                return
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
        unavailable_scopes = 0
        ok_scopes = 0
        new_events = 0
        try:
            for scope in collector.scopes():
                label = f"{collector.name}:{scope}"
                since, repair = self._window_start(watermarks.get(scope), until, label)
                result.windows[label] = (since, until)
                try:
                    events = await collector.collect_scope(scope, since=since, until=until)
                except FabricError as exc:
                    if (reason := self._unavailable_reason(collector.name, exc)) is not None:
                        self._note_unavailable(result, collector.name, label, reason)
                        unavailable_scopes += 1
                        continue
                    result.errors[label] = exc.message
                    result.units_failed += 1
                    failed_scopes += 1
                    _log.warning("collector failed", extra={"collector": label, "error": exc.to_dict()})
                    continue
                new_events += await self._store(events, result)
                result.units_ok += 1
                ok_scopes += 1
                reached = collector.progress(scope) if isinstance(collector, ResumableCollector) else None
                (repaired if repair else done)[scope] = min(reached, until) if reached else until
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
            collector.name,
            seconds=self._seconds_since(started),
            errors=failed_scopes,
            new_events=new_events,
            unavailable=unavailable_scopes > 0 and ok_scopes == 0,
        )
