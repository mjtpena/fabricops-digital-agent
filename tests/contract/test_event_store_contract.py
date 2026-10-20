"""EventStore contract: the same behaviour from SQLite and PostgreSQL (watermarks, cursor, gap-free prefix reads)."""

from __future__ import annotations

from datetime import timedelta

from tests.contract.conftest import T0, make_event

from orqentis.domain.events import EventType
from orqentis.storage.event_store import COLLECTOR_SCOPE, EventQuery, EventStore


async def test_append_dedupes_by_dedupe_key_and_by_event_id(event_store: EventStore) -> None:
    stored = await event_store.append([make_event(1), make_event(2), make_event(1)])
    assert [e.event_id for e in stored] == ["e1", "e2"]  # a duplicate inside one batch is stored once
    assert await event_store.append([make_event(1)]) == []
    assert await event_store.append([make_event(3, key="k1")]) == []  # same dedupe_key, new event id
    assert await event_store.append([]) == []
    assert await event_store.count() == 2
    assert (await event_store.get("e2")) is not None and (await event_store.get("nope")) is None


async def test_query_filters_orders_and_limits(event_store: EventStore) -> None:
    await event_store.append(
        [make_event(1), make_event(2, "b", EventType.SCHEMA_CHANGED), make_event(3), make_event(4, "b")]
    )
    assert [e.event_id for e in await event_store.query(EventQuery(item_ids=["a"]))] == ["e1", "e3"]
    assert [e.event_id for e in await event_store.query(EventQuery(workspace_ids=["w2"]))] == ["e2", "e4"]
    assert [e.event_id for e in await event_store.query(EventQuery(event_types=[EventType.SCHEMA_CHANGED]))] == ["e2"]
    assert [e.event_id for e in await event_store.query(EventQuery(correlation_id="run-1"))] == ["e1", "e3"]
    window = EventQuery(since=T0 + timedelta(minutes=2), until=T0 + timedelta(minutes=3), newest_first=True)
    assert [e.event_id for e in await event_store.query(window)] == ["e3", "e2"]  # both bounds are inclusive
    assert [e.event_id for e in await event_store.query(EventQuery(limit=2))] == ["e1", "e2"]
    assert await event_store.query(EventQuery(item_ids=[])) == []
    assert await event_store.query(EventQuery(workspace_ids=[])) == []
    assert await event_store.query(EventQuery(event_types=[])) == []


async def test_events_round_trip_exactly(event_store: EventStore) -> None:
    payload = {"n": 1, "nested": {"a": [1, 2.5, None]}, "s": "é\u0000x"}  # a NUL character in untrusted text
    original = make_event(7).model_copy(update={"payload": payload})
    await event_store.append([original])
    assert await event_store.get(original.event_id) == original


async def test_watermarks_advance_never_go_back_and_are_per_scope(event_store: EventStore) -> None:
    assert await event_store.get_watermarks("pipelines") == {}
    await event_store.advance_watermarks("pipelines", {"p1": T0, COLLECTOR_SCOPE: T0})
    await event_store.advance_watermarks("pipelines", {"p1": T0 + timedelta(hours=1), "p2": T0})
    await event_store.advance_watermarks("pipelines", {"p1": T0 - timedelta(hours=1)})  # older: ignored
    await event_store.advance_watermarks("refreshes", {"p1": T0 + timedelta(days=1)})
    await event_store.advance_watermarks("pipelines", {})
    marks = await event_store.get_watermarks("pipelines")
    assert marks == {"p1": T0 + timedelta(hours=1), "p2": T0, COLLECTOR_SCOPE: T0}
    assert await event_store.get_watermarks("refreshes") == {"p1": T0 + timedelta(days=1)}


async def test_force_repairs_a_watermark_that_is_ahead_of_the_clock(event_store: EventStore) -> None:
    await event_store.advance_watermarks("c", {"s": T0 + timedelta(days=30)})
    await event_store.advance_watermarks("c", {"s": T0}, force=True)
    assert await event_store.get_watermarks("c") == {"s": T0}


async def test_read_after_returns_events_in_insertion_order_after_a_position(event_store: EventStore) -> None:
    await event_store.append([make_event(i) for i in range(1, 6)])
    everything = await event_store.read_after(0)
    assert [s.event.event_id for s in everything] == ["e1", "e2", "e3", "e4", "e5"]
    sequences = [s.sequence for s in everything]
    assert sequences == sorted(sequences) and len(set(sequences)) == 5 and sequences[0] > 0
    after = await event_store.read_after(sequences[1])
    assert [s.event.event_id for s in after] == ["e3", "e4", "e5"]
    assert [s.event.event_id for s in await event_store.read_after(0, limit=2)] == ["e1", "e2"]
    assert await event_store.read_after(sequences[-1]) == []
    # An event stored later always gets a higher sequence than everything already read.
    await event_store.append([make_event(9)])
    assert [s.event.event_id for s in await event_store.read_after(sequences[-1])] == ["e9"]


async def test_a_duplicate_does_not_disturb_the_sequence(event_store: EventStore) -> None:
    await event_store.append([make_event(1), make_event(2)])
    first = await event_store.read_after(0)
    await event_store.append([make_event(1), make_event(3)])
    later = await event_store.read_after(first[-1].sequence)
    assert [s.event.event_id for s in later] == ["e3"] and later[0].sequence > first[-1].sequence


async def test_cursors_are_per_consumer_and_never_move_backwards(event_store: EventStore) -> None:
    assert await event_store.get_cursor("detection") == 0
    assert await event_store.advance_cursor("detection", 7) == 7
    assert await event_store.advance_cursor("detection", 3) == 7
    assert await event_store.get_cursor("detection") == 7
    assert await event_store.advance_cursor("other", 2) == 2
    assert await event_store.get_cursor("detection") == 7
