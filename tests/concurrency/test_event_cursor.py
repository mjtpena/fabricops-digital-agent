"""The detection cursor never skips an event, however appenders interleave and however late they commit.

ADR-0013 section 7 and ``EventStore.read_after``: a consumer advances its cursor to the highest sequence it was
given and never looks back, so an event may be returned only when no lower-numbered event can still become visible.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest

from orqentis.config.clock import SystemClock
from orqentis.domain.events import EventType, OperationalEvent
from orqentis.storage.postgres import PostgresConfig, PostgresDatabase, PostgresStorage

T0 = datetime(2026, 10, 5, tzinfo=UTC)


def event(n: int) -> OperationalEvent:
    return OperationalEvent(
        event_id=f"e{n}",
        event_type=EventType.PIPELINE_RUN_FAILED,
        tenant_id="t",
        item_id=f"item-{n % 7}",
        timestamp=T0 + timedelta(seconds=n),
        summary="s",
        dedupe_key=f"k{n}",
        source="test",
    )


async def test_control_a_bare_identity_column_does_skip_events(pg: PostgresStorage) -> None:
    """Why the adapter serialises appends: with a plain identity column a late commit is skipped for good."""
    db = pg.database
    await db.execute("CREATE TABLE bare_events (seq bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, v text)")
    async with db.connection() as slow, db.connection() as fast:
        slow_tx, fast_tx = slow.transaction(), fast.transaction()
        await slow_tx.start()
        await slow.execute("INSERT INTO bare_events (v) VALUES ('first: number 1, commits last')")
        await fast_tx.start()
        await fast.execute("INSERT INTO bare_events (v) VALUES ('second: number 2')")
        await fast_tx.commit()

        cursor = 0
        seen = [r["seq"] for r in await db.fetch("SELECT seq FROM bare_events WHERE seq > $1 ORDER BY seq", cursor)]
        cursor = max(seen)
        await slow_tx.commit()  # number 1 becomes visible after the consumer moved past it
        later = [r["seq"] for r in await db.fetch("SELECT seq FROM bare_events WHERE seq > $1 ORDER BY seq", cursor)]
    assert seen == [2] and later == []  # event number 1 is lost to this consumer forever


@asynccontextmanager
async def slow_commits(monkeypatch: pytest.MonkeyPatch, seed: int) -> AsyncIterator[None]:
    """Make every transaction linger a random moment between its statements and its commit."""
    rng = random.Random(seed)  # noqa: S311 (jitter for a test, not a secret)
    original = PostgresDatabase.transaction

    @asynccontextmanager
    async def lingering(self: PostgresDatabase, *, fenced: bool = True) -> AsyncIterator[object]:
        async with original(self, fenced=fenced) as conn:
            yield conn
            await asyncio.sleep(rng.uniform(0, 0.04))

    monkeypatch.setattr(PostgresDatabase, "transaction", lingering)
    yield


async def test_concurrent_appenders_on_two_instances_with_late_commits_never_make_the_cursor_skip(
    pg_config: PostgresConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = await PostgresStorage.open(pg_config, None, SystemClock())
    second = await PostgresStorage.open(pg_config, None, SystemClock(), migrate=False)  # a second app instance
    try:
        async with slow_commits(monkeypatch, seed=7):
            total = 0
            counter = iter(range(1, 10_000))

            async def appender(store_owner: PostgresStorage, batches: int) -> None:
                for _ in range(batches):
                    batch = [event(next(counter)) for _ in range(3)]
                    await store_owner.events.append(batch)

            done = asyncio.Event()
            seen: list[int] = []
            ids: list[str] = []

            async def reader() -> None:
                cursor = 0
                while True:
                    finished = done.is_set()
                    for item in await first.events.read_after(cursor, limit=7):
                        assert item.sequence > cursor, "read_after went backwards"
                        seen.append(item.sequence)
                        ids.append(item.event.event_id)
                        cursor = item.sequence  # the consumer's cursor moves to the highest sequence it was given
                    if finished and not await first.events.read_after(cursor, limit=1):
                        return
                    await asyncio.sleep(0.005)

            reading = asyncio.create_task(reader())
            await asyncio.gather(*[appender(first if n % 2 else second, 12) for n in range(6)])
            total = await first.events.count()
            done.set()
            await asyncio.wait_for(reading, 60)

        assert total == 6 * 12 * 3
        assert len(ids) == len(set(ids)) == total, "an event was skipped or delivered twice"
        assert seen == sorted(seen)
    finally:
        await first.close()
        await second.close()


async def test_a_reader_never_sees_a_higher_sequence_before_a_lower_one_has_committed(pg: PostgresStorage) -> None:
    """Deterministic: hold one appender inside its transaction and let another try to commit past it."""
    entered, release = asyncio.Event(), asyncio.Event()
    db = pg.database
    original = PostgresDatabase.transaction

    @asynccontextmanager
    async def held(self: PostgresDatabase, *, fenced: bool = True) -> AsyncIterator[object]:
        async with original(self, fenced=fenced) as conn:
            yield conn
            if not entered.is_set():  # only the first appender lingers, after its insert and before its commit
                entered.set()
                await release.wait()

    PostgresDatabase.transaction = held  # type: ignore[method-assign]
    try:
        slow = asyncio.create_task(pg.events.append([event(1)]))
        await entered.wait()
        fast = asyncio.create_task(pg.events.append([event(2)]))
        await asyncio.sleep(0.3)
        # The second appender is queued behind the first one's lock, so it has not committed anything either.
        assert not fast.done()
        assert await pg.events.read_after(0) == []
        release.set()
        await asyncio.gather(slow, fast)
    finally:
        PostgresDatabase.transaction = original  # type: ignore[method-assign]
    after = await pg.events.read_after(0)
    assert [s.event.event_id for s in after] == ["e1", "e2"]
    assert after[0].sequence < after[1].sequence
    assert await db.fetchval("SELECT COUNT(*) FROM operational_events") == 2
