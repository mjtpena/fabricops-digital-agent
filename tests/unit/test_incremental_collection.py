"""Incremental collection: per-collector and per-item watermarks, bounded windows, safe overlap, clock repair."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from orqentis.config.clock import FixedClock
from orqentis.domain.events import EventType, OperationalEvent
from orqentis.fabric.errors import FabricUnavailableError
from orqentis.storage.event_store import COLLECTOR_SCOPE, LocalEventStore
from orqentis.storage.sqlite import SqliteDatabase
from orqentis.telemetry.collectors import Collector, TelemetryIngestor

T0 = datetime(2026, 10, 20, 8, 0, tzinfo=UTC)
MINUTE = timedelta(minutes=1)
HOUR = timedelta(hours=1)


def event(key: str, at: datetime) -> OperationalEvent:
    return OperationalEvent(
        event_id=f"evt-{key}",
        event_type=EventType.PIPELINE_RUN_SUCCEEDED,
        tenant_id="tenant-test",
        timestamp=at,
        summary=key,
        dedupe_key=key,
        source="test",
    )


class RecordingCollector:
    """Returns whatever ``events`` contains inside the requested window and remembers every window."""

    def __init__(self, name: str, events: list[OperationalEvent] | None = None) -> None:
        self.name = name
        self.events = events or []
        self.windows: list[tuple[datetime, datetime]] = []
        self.fail = False

    async def collect(self, *, since: datetime, until: datetime) -> list[OperationalEvent]:
        self.windows.append((since, until))
        if self.fail:
            raise FabricUnavailableError("Fabric is down", status_code=503, error_code="Unavailable", retryable=True)
        return [e for e in self.events if since <= e.timestamp <= until]


class ScopedRecorder:
    """A collector over several scopes (pipelines); the scope list can change between runs, like the catalog."""

    def __init__(self, name: str, scopes: list[str]) -> None:
        self.name = name
        self._scopes = scopes
        self.windows: dict[str, list[tuple[datetime, datetime]]] = {}
        self.failing: set[str] = set()
        self.events: dict[str, list[OperationalEvent]] = {}

    def scopes(self) -> list[str]:
        return list(self._scopes)

    def add_scope(self, scope: str) -> None:
        self._scopes.append(scope)

    async def collect_scope(self, scope: str, *, since: datetime, until: datetime) -> list[OperationalEvent]:
        self.windows.setdefault(scope, []).append((since, until))
        if scope in self.failing:
            raise FabricUnavailableError("Fabric is down", status_code=503, error_code="Unavailable", retryable=True)
        return [e for e in self.events.get(scope, []) if since <= e.timestamp <= until]


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[LocalEventStore]:
    db = SqliteDatabase(tmp_path / "events.sqlite3")
    yield LocalEventStore(db)
    db.close()


def ingestor(
    collectors: list[Collector], store: LocalEventStore, clock: FixedClock, **kwargs: timedelta
) -> TelemetryIngestor:
    kwargs.setdefault("lookback", 7 * 24 * HOUR)
    kwargs.setdefault("overlap", 15 * MINUTE)
    return TelemetryIngestor(collectors, store, clock, **kwargs)


# ----------------------------------------------------------------------------------------- watermarks
async def test_watermarks_start_empty_and_never_move_backwards(store: LocalEventStore) -> None:
    assert await store.get_watermarks("pipeline-runs") == {}
    await store.advance_watermarks("pipeline-runs", {COLLECTOR_SCOPE: T0})
    await store.advance_watermarks("pipeline-runs", {COLLECTOR_SCOPE: T0 + HOUR})
    await store.advance_watermarks("pipeline-runs", {COLLECTOR_SCOPE: T0})  # an older value is ignored
    assert await store.get_watermarks("pipeline-runs") == {COLLECTOR_SCOPE: T0 + HOUR}
    assert await store.get_watermarks("refreshes") == {}  # per collector


async def test_watermarks_are_kept_per_scope_and_a_forced_value_may_move_back(store: LocalEventStore) -> None:
    await store.advance_watermarks("pipeline-runs", {"pl-1": T0 + HOUR, "pl-2": T0})
    await store.advance_watermarks("pipeline-runs", {"pl-1": T0, "pl-2": T0 + HOUR})
    assert await store.get_watermarks("pipeline-runs") == {"pl-1": T0 + HOUR, "pl-2": T0 + HOUR}
    await store.advance_watermarks("pipeline-runs", {"pl-1": T0}, force=True)  # repairing a clock that ran ahead
    assert (await store.get_watermarks("pipeline-runs"))["pl-1"] == T0
    await store.advance_watermarks("pipeline-runs", {})  # nothing to do is fine


async def test_watermarks_are_persisted_across_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "events.sqlite3"
    first = SqliteDatabase(path)
    await LocalEventStore(first).advance_watermarks("pipeline-runs", {"pl-1": T0, COLLECTOR_SCOPE: T0 + HOUR})
    first.close()

    reopened = SqliteDatabase(path)
    try:
        assert await LocalEventStore(reopened).get_watermarks("pipeline-runs") == {
            "pl-1": T0,
            COLLECTOR_SCOPE: T0 + HOUR,
        }
    finally:
        reopened.close()


# ------------------------------------------------------------------------------------------ windows
async def test_first_run_uses_the_maximum_initial_lookback(store: LocalEventStore) -> None:
    clock = FixedClock(T0)
    collector = RecordingCollector("pipeline-runs")
    result = await ingestor([collector], store, clock, lookback=2 * 24 * HOUR).run()
    assert collector.windows == [(T0 - 2 * 24 * HOUR, T0)]
    assert result.windows["pipeline-runs"] == (T0 - 2 * 24 * HOUR, T0)


async def test_later_runs_start_at_the_watermark_minus_the_overlap(store: LocalEventStore) -> None:
    clock = FixedClock(T0)
    collector = RecordingCollector("pipeline-runs")
    runner = ingestor([collector], store, clock)
    await runner.run()
    assert await store.get_watermarks("pipeline-runs") == {COLLECTOR_SCOPE: T0}

    clock.advance(5 * MINUTE)
    await runner.run()
    assert collector.windows[1] == (T0 - 15 * MINUTE, T0 + 5 * MINUTE)
    assert await store.get_watermarks("pipeline-runs") == {COLLECTOR_SCOPE: T0 + 5 * MINUTE}


async def test_a_stale_watermark_cannot_widen_the_window_beyond_the_maximum_lookback(store: LocalEventStore) -> None:
    clock = FixedClock(T0)
    await store.advance_watermarks("pipeline-runs", {COLLECTOR_SCOPE: T0 - 30 * 24 * HOUR})  # down for a month
    collector = RecordingCollector("pipeline-runs")
    await ingestor([collector], store, clock, lookback=7 * 24 * HOUR).run()
    assert collector.windows == [(T0 - 7 * 24 * HOUR, T0)]


async def test_each_collector_has_its_own_watermark(store: LocalEventStore) -> None:
    clock = FixedClock(T0)
    runs, refreshes = RecordingCollector("pipeline-runs"), RecordingCollector("refreshes")
    runner = ingestor([runs], store, clock)
    await runner.run()  # only pipeline-runs exists yet

    clock.advance(HOUR)
    await ingestor([runs, refreshes], store, clock).run()
    assert runs.windows[1][0] == T0 - 15 * MINUTE  # continues from its watermark
    assert refreshes.windows[0][0] == T0 + HOUR - 7 * 24 * HOUR  # a new collector starts from the full lookback


async def test_a_failed_collector_keeps_its_watermark_and_retries_the_same_window(store: LocalEventStore) -> None:
    clock = FixedClock(T0)
    good, flaky = RecordingCollector("good"), RecordingCollector("flaky")
    runner = ingestor([good, flaky], store, clock)
    await runner.run()

    flaky.fail = True
    clock.advance(10 * MINUTE)
    result = await runner.run()
    assert "flaky" in result.errors and "good" not in result.errors
    assert await store.get_watermarks("good") == {COLLECTOR_SCOPE: T0 + 10 * MINUTE}  # the healthy one moved on
    assert await store.get_watermarks("flaky") == {COLLECTOR_SCOPE: T0}  # the failing one did not

    flaky.fail = False
    clock.advance(10 * MINUTE)
    await runner.run()
    assert flaky.windows[-1] == (T0 - 15 * MINUTE, T0 + 20 * MINUTE)  # nothing was skipped
    assert good.windows[-1][0] == T0 + 10 * MINUTE - 15 * MINUTE


async def test_the_overlap_rereads_events_without_duplicating_them(store: LocalEventStore) -> None:
    clock = FixedClock(T0)
    late = event("late-arrival", T0 - 5 * MINUTE)
    collector = RecordingCollector("pipeline-runs", [late])
    runner = ingestor([collector], store, clock)

    first = await runner.run()
    assert [e.dedupe_key for e in first.new_events] == ["late-arrival"]

    clock.advance(5 * MINUTE)
    second = await runner.run()  # the window overlaps the first one, so the event is read again...
    assert second.collected == 1
    assert second.new_events == []  # ...but it is not new
    assert await store.count() == 1


async def test_an_event_that_appears_inside_the_overlap_is_still_collected(store: LocalEventStore) -> None:
    clock = FixedClock(T0)
    collector = RecordingCollector("pipeline-runs")
    runner = ingestor([collector], store, clock)
    await runner.run()

    clock.advance(10 * MINUTE)
    # Reported late: it happened 12 minutes before this run, i.e. before the previous watermark.
    collector.events.append(event("reported-late", T0 - 2 * MINUTE))
    result = await runner.run()
    assert [e.dedupe_key for e in result.new_events] == ["reported-late"]


async def test_collection_resumes_from_the_persisted_watermark_after_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "events.sqlite3"
    clock = FixedClock(T0)
    db = SqliteDatabase(path)
    await ingestor([RecordingCollector("pipeline-runs")], LocalEventStore(db), clock).run()
    db.close()

    clock.advance(20 * MINUTE)
    db = SqliteDatabase(path)  # a new process: new store object, new ingestor, same file
    try:
        collector = RecordingCollector("pipeline-runs")
        await ingestor([collector], LocalEventStore(db), clock).run()
        assert collector.windows == [(T0 - 15 * MINUTE, T0 + 20 * MINUTE)]
    finally:
        db.close()


def test_invalid_windows_are_rejected(store: LocalEventStore) -> None:
    clock = FixedClock(T0)
    with pytest.raises(ValueError, match="lookback"):
        TelemetryIngestor([], store, clock, lookback=timedelta(0))
    with pytest.raises(ValueError, match="overlap"):
        TelemetryIngestor([], store, clock, overlap=timedelta(minutes=-1))


# ------------------------------------------------------------------- per-scope watermarks (review finding 3)
async def test_every_scope_has_its_own_window_and_a_new_scope_starts_from_the_maximum_lookback(
    store: LocalEventStore,
) -> None:
    clock = FixedClock(T0)
    pipelines = ScopedRecorder("pipeline-runs", ["pl-old"])
    runner = ingestor([pipelines], store, clock)
    await runner.run()
    assert await store.get_watermarks("pipeline-runs") == {"pl-old": T0}

    # A pipeline shows up (a catalog refresh): it must not inherit the collector's progress.
    clock.advance(30 * MINUTE)
    pipelines.add_scope("pl-new")
    result = await runner.run()
    assert pipelines.windows["pl-old"][1][0] == T0 - 15 * MINUTE
    assert pipelines.windows["pl-new"] == [(T0 + 30 * MINUTE - 7 * 24 * HOUR, T0 + 30 * MINUTE)]
    assert set(result.windows) == {"pipeline-runs:pl-old", "pipeline-runs:pl-new"}
    assert set(await store.get_watermarks("pipeline-runs")) == {"pl-old", "pl-new"}


async def test_a_scope_that_failed_keeps_its_watermark_and_does_not_block_the_others(store: LocalEventStore) -> None:
    clock = FixedClock(T0)
    pipelines = ScopedRecorder("pipeline-runs", ["pl-1", "pl-2"])
    runner = ingestor([pipelines], store, clock)
    await runner.run()

    pipelines.failing = {"pl-1"}
    pipelines.events["pl-2"] = [event("pl-2-run", T0 + 5 * MINUTE)]
    clock.advance(10 * MINUTE)
    result = await runner.run()
    assert set(result.errors) == {"pipeline-runs:pl-1"}
    assert [e.dedupe_key for e in result.new_events] == ["pl-2-run"]  # the neighbour was still collected
    assert await store.get_watermarks("pipeline-runs") == {"pl-1": T0, "pl-2": T0 + 10 * MINUTE}


async def test_scope_watermarks_survive_a_restart_so_a_known_scope_is_not_reread_from_the_start(
    tmp_path: Path,
) -> None:
    path = tmp_path / "events.sqlite3"
    clock = FixedClock(T0)
    db = SqliteDatabase(path)
    await ingestor([ScopedRecorder("pipeline-runs", ["pl-1"])], LocalEventStore(db), clock).run()
    db.close()

    clock.advance(HOUR)
    db = SqliteDatabase(path)
    try:
        pipelines = ScopedRecorder("pipeline-runs", ["pl-1", "pl-2"])
        await ingestor([pipelines], LocalEventStore(db), clock).run()
        assert pipelines.windows["pl-1"][0][0] == T0 - 15 * MINUTE
        assert pipelines.windows["pl-2"][0][0] == clock.now() - 7 * 24 * HOUR  # never collected: full lookback
    finally:
        db.close()


# ----------------------------------------------------------------------------- clock repair (review minor 8)
async def test_a_watermark_ahead_of_the_clock_is_not_trusted_and_is_corrected(
    store: LocalEventStore, caplog: pytest.LogCaptureFixture
) -> None:
    clock = FixedClock(T0)
    await store.advance_watermarks("pipeline-runs", {COLLECTOR_SCOPE: T0 + 2 * HOUR})  # the clock used to run ahead
    collector = RecordingCollector("pipeline-runs", [event("older-than-the-bogus-watermark", T0 - HOUR)])
    with caplog.at_level("WARNING", logger="orqentis"):
        result = await ingestor([collector], store, clock).run()
    # Re-read the full lookback instead of an empty or inverted window, so nothing in the gap is missed...
    assert collector.windows == [(T0 - 7 * 24 * HOUR, T0)]
    assert [e.dedupe_key for e in result.new_events] == ["older-than-the-bogus-watermark"]
    # ...say so...
    assert any("ahead of the clock" in r.getMessage() for r in caplog.records)
    # ...and repair it, so the next run is back to the normal window.
    assert await store.get_watermarks("pipeline-runs") == {COLLECTOR_SCOPE: T0}
    clock.advance(5 * MINUTE)
    await ingestor([collector], store, clock).run()
    assert collector.windows[-1] == (T0 - 15 * MINUTE, T0 + 5 * MINUTE)


# ------------------------------------------------------------------- durable consumption (review finding 2)
async def test_events_are_numbered_in_insertion_order_and_a_duplicate_takes_no_number(store: LocalEventStore) -> None:
    await store.append([event("a", T0), event("b", T0 - HOUR)])  # timestamp order differs from insertion order
    await store.append([event("a", T0), event("c", T0)])  # "a" is a duplicate
    batch = await store.read_after(0)
    assert [(item.event.dedupe_key) for item in batch] == ["a", "b", "c"]
    sequences = [item.sequence for item in batch]
    assert sequences == sorted(sequences) and len(set(sequences)) == 3

    assert [item.event.dedupe_key for item in await store.read_after(sequences[0])] == ["b", "c"]
    assert [item.event.dedupe_key for item in await store.read_after(0, limit=2)] == ["a", "b"]
    assert await store.read_after(sequences[-1]) == []


async def test_consumer_cursors_are_independent_never_move_backwards_and_survive_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "events.sqlite3"
    db = SqliteDatabase(path)
    first = LocalEventStore(db)
    assert await first.get_cursor("detection") == 0
    assert await first.advance_cursor("detection", 7) == 7
    assert await first.advance_cursor("detection", 3) == 7  # an older position is ignored
    assert await first.get_cursor("other") == 0
    db.close()

    db = SqliteDatabase(path)
    try:
        assert await LocalEventStore(db).get_cursor("detection") == 7
    finally:
        db.close()
