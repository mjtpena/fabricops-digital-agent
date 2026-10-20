"""Leader election across instances: a single scheduler leader, handover on failure, and fenced writes."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from tests.support.polling import wait_until

from orqentis.config.clock import SystemClock
from orqentis.domain.events import EventType, OperationalEvent
from orqentis.runtime.fencing import Fence, FencingError, fenced
from orqentis.runtime.scheduler import JobContext, JobSpec, Scheduler
from orqentis.storage.event_store import EventQuery
from orqentis.storage.postgres import PostgresConfig, PostgresLeaderLease, PostgresStorage

LEASE = "orqentis-scheduler"
TTL = timedelta(seconds=2)


def event(n: int) -> OperationalEvent:
    return OperationalEvent(
        event_id=f"e{n}",
        event_type=EventType.PIPELINE_RUN_FAILED,
        tenant_id="t",
        item_id="item",
        timestamp=datetime(2026, 10, 5, tzinfo=UTC) + timedelta(seconds=n),
        summary="s",
        dedupe_key=f"k{n}",
        source="test",
    )


async def open_instance(config: PostgresConfig, holder: str) -> PostgresStorage:
    return await PostgresStorage.open(config, None, SystemClock(), migrate=False, holder_id=holder)


async def test_a_deposed_leaders_late_write_is_refused_but_other_writes_are_not(
    pg: PostgresStorage, pg_config: PostgresConfig
) -> None:
    second = await open_instance(pg_config, "replica-b")
    try:
        old = await pg.lease.acquire(LEASE, TTL)
        assert old is not None
        old_fence = Fence(LEASE, old.holder_id, old.fencing_token)
        with fenced(old_fence):
            await pg.events.append([event(1)])  # the leader writes
        await asyncio.sleep(2.3)  # the leader stalls past its TTL (a long pause, a partition)
        new = await second.lease.acquire(LEASE, TTL)
        assert new is not None and new.fencing_token == old.fencing_token + 1

        with fenced(old_fence), pytest.raises(FencingError, match="no longer held"):
            await pg.events.append([event(2)])  # it wakes up and carries on: the database refuses
        with fenced(old_fence), pytest.raises(FencingError):
            await pg.events.advance_cursor("detection", 5)
        with fenced(old_fence), pytest.raises(FencingError):
            await pg.incidents.next_incident_id(2026)
        assert [e.event_id for e in await second.events.query(EventQuery())] == ["e1"]
        assert await second.events.get_cursor("detection") == 0

        with fenced(Fence(LEASE, new.holder_id, new.fencing_token)):
            await second.events.append([event(3)])  # the new leader may write
        await pg.events.append([event(4)])  # a request (no fence) is never refused by the fence
        audit = await pg.audit.record(actor="old-leader", action="late", outcome="OK")  # nor is an audit record
        assert audit.sequence == 1
        assert await pg.events.count() == 3
    finally:
        await second.close()


async def test_a_fence_for_a_released_or_unknown_lease_refuses_writes(pg: PostgresStorage) -> None:
    lease = await pg.lease.acquire(LEASE, TTL)
    assert lease is not None
    await pg.lease.release(lease)
    with fenced(Fence(LEASE, lease.holder_id, lease.fencing_token)), pytest.raises(FencingError):
        await pg.events.append([event(1)])
    with fenced(Fence("never-existed", "x", 1)), pytest.raises(FencingError):
        await pg.events.append([event(1)])


async def test_a_takeover_waits_for_a_fenced_write_in_flight_and_that_write_completes(
    pg: PostgresStorage, pg_config: PostgresConfig
) -> None:
    """The check and the write are one unit: the lease cannot change hands in between."""
    second = await open_instance(pg_config, "replica-b")
    try:
        old = await pg.lease.acquire(LEASE, timedelta(seconds=1))
        assert old is not None
        await asyncio.sleep(1.3)  # expired: replica-b is entitled to take it
        inside, finish = asyncio.Event(), asyncio.Event()

        async def slow_fenced_write() -> None:
            with fenced(Fence(LEASE, old.holder_id, old.fencing_token)):
                async with pg.database.transaction() as conn:  # the fence is checked (and the lease row share-locked)
                    await conn.execute("SELECT 1")
                    inside.set()
                    await finish.wait()
                    await conn.execute(
                        "INSERT INTO consumer_cursors (consumer, position) VALUES ('written-under-the-old-fence', 1)"
                    )

        writer = asyncio.create_task(slow_fenced_write())
        await inside.wait()
        takeover = asyncio.create_task(second.lease.acquire(LEASE, TTL))
        await asyncio.sleep(0.4)
        assert not takeover.done()  # the takeover waits for the write that was already checked
        finish.set()
        await writer
        new = await asyncio.wait_for(takeover, 10)
        assert new is not None and new.fencing_token == old.fencing_token + 1
        assert await pg.events.get_cursor("written-under-the-old-fence") == 1
        with fenced(Fence(LEASE, old.holder_id, old.fencing_token)), pytest.raises(FencingError):
            await pg.events.advance_cursor("another", 1)  # but the next one, after the handover, is refused
    finally:
        await second.close()


async def test_only_one_scheduler_runs_jobs_and_the_other_takes_over_when_the_leader_dies(
    pg: PostgresStorage, pg_config: PostgresConfig
) -> None:
    other = await open_instance(pg_config, "replica-b")
    log: list[tuple[str, int]] = []
    numbers = iter(range(1, 10_000))

    def scheduler_for(storage: PostgresStorage, name: str) -> Scheduler:
        async def job(ctx: JobContext) -> None:
            await storage.events.append([event(next(numbers))])  # runs under the job's fence
            log.append((name, ctx.fencing_token))

        return Scheduler(
            [JobSpec("tick-job", timedelta(seconds=0.2), job)],
            storage.lease,
            SystemClock(),
            lease_name=LEASE,
            lease_ttl=timedelta(seconds=3),
            tick_interval=timedelta(seconds=0.5),
            sleep=asyncio.sleep,
        )

    a, b = scheduler_for(pg, "a"), scheduler_for(other, "b")
    try:
        await a.start()
        await wait_until(lambda: a.is_leader, within=20)
        await b.start()
        await wait_until(lambda: len(log) >= 2, within=20)
        await asyncio.sleep(2.0)  # b ticks several times; the lease is not free, so it must stay a follower
        assert a.is_leader and not b.is_leader
        assert {name for name, _ in log} == {"a"}  # exactly one instance ran the job

        leader_generation = log[0][1]
        # The leader dies without releasing the lease (kill -9): cancel its loop and jobs, keep the lease row.
        loop_task = a._loop_task
        assert loop_task is not None
        loop_task.cancel()
        await asyncio.gather(loop_task, return_exceptions=True)
        await a._cancel_running()
        crashed_at = len(log)
        await wait_until(lambda: b.is_leader, within=30)  # the lease lapses (3 s) and b's next tick acquires it
        await wait_until(lambda: len(log) > crashed_at, within=20)
        later = log[crashed_at:]
        assert {name for name, _ in later} == {"b"}
        assert all(token == leader_generation + 1 for _, token in later)  # b's tenure has the next fencing token
        assert await pg.events.count() >= len(log)
    finally:
        await b.stop()
        await a.stop()
        await other.close()


async def test_many_instances_racing_for_the_lease_produce_one_leader(
    pg: PostgresStorage, pg_config: PostgresConfig
) -> None:
    instances = [await open_instance(pg_config, f"replica-{n}") for n in range(8)]
    try:
        results = await asyncio.gather(*(i.lease.acquire(LEASE, TTL) for i in instances))
        assert len([r for r in results if r is not None]) == 1
        lease = PostgresLeaderLease(pg.database, SystemClock(), holder_id="observer")
        assert await lease.acquire(LEASE, TTL) is None
    finally:
        for instance in instances:
            await instance.close()


async def test_renewing_and_taking_over_are_not_starved_by_a_stream_of_fenced_writes(
    pg: PostgresStorage, pg_config: PostgresConfig
) -> None:
    """Reproduced: with six or more writers back to back, a renewal waited 8 s or more behind row-level share locks."""
    second = await open_instance(pg_config, "replica-b")
    busy = await open_instance(replace(pg_config, pool_max=40), "replica-a-writers")  # room for many overlapping writes
    lease = await pg.lease.acquire(LEASE, timedelta(seconds=60))
    assert lease is not None
    fence = Fence(LEASE, lease.holder_id, lease.fencing_token)
    stop = asyncio.Event()
    refused = asyncio.Event()

    async def writer() -> None:
        with fenced(fence):
            while not stop.is_set():
                try:
                    async with busy.database.transaction() as conn:
                        await conn.execute("SELECT pg_sleep(0.05)")  # a write that takes a little time
                except FencingError:
                    refused.set()
                    return

    writers = [asyncio.create_task(writer()) for _ in range(30)]
    loop = asyncio.get_running_loop()
    try:
        await asyncio.sleep(0.3)  # the writers are saturating the lease
        latencies: list[float] = []
        for _ in range(8):
            started = loop.time()
            renewed = await asyncio.wait_for(pg.lease.renew(lease, timedelta(seconds=60)), 20)
            latencies.append(loop.time() - started)
            assert renewed is not None
            await asyncio.sleep(0.1)
        assert max(latencies) < 1.5, latencies  # was 8 s and more

        # A takeover (after expiry) is not starved either, and the old leader's writers are then refused.
        await pg.database.execute("UPDATE leases SET expires_at = now() - interval '1 minute'")
        started = loop.time()
        taken = await asyncio.wait_for(second.lease.acquire(LEASE, TTL), 20)
        assert taken is not None and taken.fencing_token == lease.fencing_token + 1
        assert loop.time() - started < 2.5
        await asyncio.wait_for(refused.wait(), 10)
    finally:
        stop.set()
        await asyncio.gather(*writers, return_exceptions=True)
        await busy.close()
        await second.close()
