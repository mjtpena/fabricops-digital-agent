"""Incremental collection: per-collector watermarks, bounded windows, safe overlap."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from orqentis.config.clock import FixedClock
from orqentis.domain.events import EventType, OperationalEvent
from orqentis.fabric.errors import FabricUnavailableError
from orqentis.storage.event_store import LocalEventStore
from orqentis.storage.sqlite import SqliteDatabase
from orqentis.telemetry.collectors import TelemetryIngestor

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


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[LocalEventStore]:
    db = SqliteDatabase(tmp_path / "events.sqlite3")
    yield LocalEventStore(db)
    db.close()


def ingestor(
    collectors: list[RecordingCollector], store: LocalEventStore, clock: FixedClock, **kwargs: timedelta
) -> TelemetryIngestor:
    kwargs.setdefault("lookback", 7 * 24 * HOUR)
    kwargs.setdefault("overlap", 15 * MINUTE)
    return TelemetryIngestor(collectors, store, clock, **kwargs)


# ----------------------------------------------------------------------------------------- watermarks
async def test_watermarks_start_empty_and_never_move_backwards(store: LocalEventStore) -> None:
    assert await store.get_watermark("pipeline-runs") is None
    assert await store.advance_watermark("pipeline-runs", T0) == T0
    assert await store.advance_watermark("pipeline-runs", T0 + HOUR) == T0 + HOUR
    assert await store.advance_watermark("pipeline-runs", T0) == T0 + HOUR  # an older value is ignored
    assert await store.get_watermark("pipeline-runs") == T0 + HOUR
    assert await store.get_watermark("refreshes") is None  # per collector


async def test_watermarks_are_persisted_across_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "events.sqlite3"
    first = SqliteDatabase(path)
    await LocalEventStore(first).advance_watermark("pipeline-runs", T0)
    first.close()

    reopened = SqliteDatabase(path)
    try:
        assert await LocalEventStore(reopened).get_watermark("pipeline-runs") == T0
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
    assert await store.get_watermark("pipeline-runs") == T0

    clock.advance(5 * MINUTE)
    await runner.run()
    assert collector.windows[1] == (T0 - 15 * MINUTE, T0 + 5 * MINUTE)
    assert await store.get_watermark("pipeline-runs") == T0 + 5 * MINUTE


async def test_a_stale_watermark_cannot_widen_the_window_beyond_the_maximum_lookback(store: LocalEventStore) -> None:
    clock = FixedClock(T0)
    await store.advance_watermark("pipeline-runs", T0 - 30 * 24 * HOUR)  # the app was down for a month
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
    assert await store.get_watermark("good") == T0 + 10 * MINUTE  # the healthy collector moved on
    assert await store.get_watermark("flaky") == T0  # the failing one did not

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
    assert second.new_events == []  # ...but it is not new, so detection does not see it twice
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
