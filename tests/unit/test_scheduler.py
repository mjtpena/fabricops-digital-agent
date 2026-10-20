"""Scheduler: schedule, leadership, no overlap, failure isolation. Time is a fake clock, never a real wait."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from orqentis.config.clock import FixedClock
from orqentis.runtime.lease import InProcessLeaderLease, Lease, LeaseTable
from orqentis.runtime.scheduler import JobSpec, Scheduler
from tests.support.polling import wait_until

T0 = datetime(2026, 10, 20, 8, 0, tzinfo=UTC)
MINUTE = timedelta(minutes=1)
LONG_TTL = timedelta(hours=1)  # the fake clock jumps minutes at a time; the lease must outlive the jumps


class Counter:
    """A job that records every run and how many runs were in flight at once."""

    def __init__(self) -> None:
        self.runs = 0
        self.active = 0
        self.max_active = 0
        self.gate: asyncio.Event | None = None

    async def __call__(self) -> None:
        self.runs += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.gate is not None:
                await self.gate.wait()
        finally:
            self.active -= 1


class FakeAudit:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    async def record(self, **kwargs: Any) -> None:
        self.records.append(kwargs)


def scheduler(
    jobs: list[JobSpec],
    clock: FixedClock,
    *,
    holder: str = "replica-a",
    table: LeaseTable | None = None,
    **kwargs: Any,
) -> Scheduler:
    lease = InProcessLeaderLease(clock, holder_id=holder, table=table)
    kwargs.setdefault("lease_ttl", LONG_TTL)
    return Scheduler(jobs, lease, clock, **kwargs)


async def step(sched: Scheduler, clock: FixedClock, by: timedelta = timedelta(0)) -> list[str]:
    clock.advance(by)
    started = await sched.tick()
    await sched.wait_idle()
    return started


async def test_each_job_runs_on_its_own_schedule() -> None:
    clock = FixedClock(T0)
    fast, slow = Counter(), Counter()
    sched = scheduler(
        [JobSpec("fast", 5 * MINUTE, fast), JobSpec("slow", 15 * MINUTE, slow)],
        clock,
    )
    assert sorted(await step(sched, clock)) == ["fast", "slow"]  # both are due on the first tick
    assert await step(sched, clock, 4 * MINUTE) == []
    assert await step(sched, clock, 1 * MINUTE) == ["fast"]  # minute 5
    assert await step(sched, clock, 5 * MINUTE) == ["fast"]  # minute 10
    assert sorted(await step(sched, clock, 5 * MINUTE)) == ["fast", "slow"]  # minute 15
    assert (fast.runs, slow.runs) == (4, 2)
    status = {j.name: j for j in sched.status().jobs}
    assert status["fast"].run_count == 4 and status["fast"].last_outcome == "ok"
    assert status["fast"].next_run_at == T0 + 20 * MINUTE


async def test_a_job_can_wait_one_interval_before_its_first_run() -> None:
    clock = FixedClock(T0)
    job = Counter()
    sched = scheduler([JobSpec("later", 10 * MINUTE, job, run_immediately=False)], clock)
    assert await step(sched, clock) == []
    assert await step(sched, clock, 9 * MINUTE) == []
    assert await step(sched, clock, 1 * MINUTE) == ["later"]
    assert job.runs == 1


async def test_disabled_jobs_never_run() -> None:
    clock = FixedClock(T0)
    off, on = Counter(), Counter()
    sched = scheduler([JobSpec("off", MINUTE, off, enabled=False), JobSpec("on", MINUTE, on)], clock)
    for _ in range(3):
        await step(sched, clock, MINUTE)
    assert (off.runs, on.runs) == (0, 3)


async def test_only_the_lease_holder_runs_jobs() -> None:
    clock = FixedClock(T0)
    table = LeaseTable()
    leader_job, standby_job = Counter(), Counter()
    leader = scheduler([JobSpec("work", MINUTE, leader_job)], clock, holder="replica-a", table=table)
    standby = scheduler([JobSpec("work", MINUTE, standby_job)], clock, holder="replica-b", table=table)

    for _ in range(3):
        await step(leader, clock, MINUTE)
        assert await step(standby, clock) == []
    assert (leader_job.runs, standby_job.runs) == (3, 0)
    assert leader.is_leader and not standby.is_leader
    assert leader.status().fencing_token == 1 and standby.status().fencing_token is None

    # A clean hand-over: the leader stops (releasing the lease) and the standby takes over with a newer token.
    await leader.stop()
    assert await step(standby, clock, MINUTE) == ["work"]
    assert standby.is_leader and standby.status().fencing_token == 2
    assert standby_job.runs == 1


async def test_a_deposed_leader_stops_and_cancels_its_running_job() -> None:
    clock = FixedClock(T0)
    table = LeaseTable()
    ttl = timedelta(minutes=2)
    stuck = Counter()
    stuck.gate = asyncio.Event()  # never opened: the job is in flight when leadership changes
    old = scheduler([JobSpec("work", MINUTE, stuck)], clock, holder="replica-a", table=table, lease_ttl=ttl)
    new_job = Counter()
    new = scheduler([JobSpec("work", MINUTE, new_job)], clock, holder="replica-b", table=table, lease_ttl=ttl)

    assert await old.tick() == ["work"]
    await asyncio.sleep(0)
    assert stuck.active == 1

    # replica-a stalls past its lease; replica-b takes over.
    clock.advance(ttl + MINUTE)
    assert await step(new, clock) == ["work"]
    assert new.is_leader

    # replica-a wakes up: it cannot renew, so it steps down and its in-flight job is cancelled.
    assert await old.tick() == []
    await old.wait_idle()
    assert not old.is_leader
    assert stuck.active == 0
    assert old.status().jobs[0].last_outcome == "cancelled"
    assert new_job.runs == 1


async def test_a_job_never_overlaps_with_itself_even_when_a_run_outlasts_its_interval() -> None:
    clock = FixedClock(T0)
    slow = Counter()
    slow.gate = asyncio.Event()
    sched = scheduler([JobSpec("slow", 5 * MINUTE, slow)], clock)

    assert await sched.tick() == ["slow"]
    await asyncio.sleep(0)  # let the run begin
    for _ in range(3):  # three whole intervals pass while the first run is still going
        clock.advance(5 * MINUTE)
        assert await sched.tick() == []
    job = sched.status().jobs[0]
    assert (slow.runs, slow.max_active, job.skipped_overlaps, job.running) == (1, 1, 3, True)

    slow.gate.set()
    await sched.wait_idle()
    assert not sched.status().jobs[0].running

    # Slots missed meanwhile collapse into one run; there is no burst of catch-up runs.
    clock.advance(60 * MINUTE)
    assert await sched.tick() == ["slow"]
    assert await sched.tick() == []
    await sched.wait_idle()
    assert (slow.runs, slow.max_active) == (2, 1)


async def test_a_failing_job_is_isolated_audited_once_and_does_not_stop_the_scheduler() -> None:
    clock = FixedClock(T0)
    audit = FakeAudit()
    healthy = Counter()
    attempts = 0

    async def flaky() -> None:
        nonlocal attempts
        attempts += 1
        if attempts <= 3:
            raise RuntimeError("upstream said no: password=hunter2")

    sched = scheduler([JobSpec("flaky", MINUTE, flaky), JobSpec("healthy", MINUTE, healthy)], clock, audit=audit)
    for minute in range(5):
        await step(sched, clock, MINUTE if minute else timedelta(0))

    assert healthy.runs == 5  # unaffected by its neighbour
    status = {j.name: j for j in sched.status().jobs}
    assert attempts == 5 and status["flaky"].failure_count == 3
    assert status["flaky"].consecutive_failures == 0 and status["flaky"].last_outcome == "ok"
    # Health *changes* are audited, not every failed attempt: one FAILED, then one RECOVERED.
    job_audit = [(r["outcome"], r["details"].get("job")) for r in audit.records if r["action"] == "scheduler.job"]
    assert job_audit == [("FAILED", "flaky"), ("RECOVERED", "flaky")]
    failed = next(r for r in audit.records if r["outcome"] == "FAILED")
    assert "hunter2" not in failed["details"]["error"] and "RuntimeError" in failed["details"]["error"]


async def test_failure_details_exposed_on_status_are_redacted() -> None:
    clock = FixedClock(T0)

    async def boom() -> None:
        raise ValueError("call failed with token=abc123secret")

    sched = scheduler([JobSpec("boom", MINUTE, boom)], clock)
    await step(sched, clock)
    job = sched.status().jobs[0]
    assert job.last_outcome == "failed" and job.consecutive_failures == 1
    assert job.last_error is not None and "abc123secret" not in job.last_error


async def test_a_job_that_runs_too_long_is_cancelled_and_reported() -> None:
    clock = FixedClock(T0)
    other = Counter()

    async def hang() -> None:
        await asyncio.sleep(30)

    sched = scheduler(
        [JobSpec("hang", MINUTE, hang, timeout=timedelta(milliseconds=50)), JobSpec("other", MINUTE, other)], clock
    )
    await step(sched, clock)
    job = {j.name: j for j in sched.status().jobs}["hang"]
    assert job.last_outcome == "timeout" and job.failure_count == 1 and not job.running
    assert other.runs == 1


async def test_lease_backend_errors_make_the_scheduler_a_follower_not_a_crash() -> None:
    clock = FixedClock(T0)
    job = Counter()

    class BrokenLease:
        holder_id = "broken"

        async def acquire(self, name: str, ttl: timedelta) -> Lease | None:
            raise ConnectionError("lease database unreachable")

        async def renew(self, lease: Lease, ttl: timedelta) -> Lease | None:
            raise ConnectionError("lease database unreachable")

        async def release(self, lease: Lease) -> None:
            return None

    sched = Scheduler([JobSpec("work", MINUTE, job)], BrokenLease(), clock)
    assert await sched.tick() == []
    assert job.runs == 0 and not sched.is_leader


async def test_the_background_loop_ticks_until_stopped_and_then_releases_the_lease() -> None:
    clock = FixedClock(T0)
    table = LeaseTable()
    job = Counter()

    async def tick_sleep(_: float) -> None:
        clock.advance(MINUTE)  # each loop iteration is one fake minute
        await asyncio.sleep(0.001)

    sched = scheduler([JobSpec("work", MINUTE, job)], clock, table=table, sleep=tick_sleep)
    await sched.start()
    assert sched.is_running
    await wait_until(lambda: job.runs >= 3, within=5)
    await sched.stop()
    assert not sched.is_running and not sched.is_leader
    # The released lease is immediately available to another replica.
    assert await InProcessLeaderLease(clock, holder_id="replica-b", table=table).acquire("orqentis-scheduler", LONG_TTL)


def test_duplicate_job_names_and_non_positive_intervals_are_rejected() -> None:
    clock = FixedClock(T0)
    lease = InProcessLeaderLease(clock)
    job = Counter()
    with pytest.raises(ValueError, match="unique"):
        Scheduler([JobSpec("a", MINUTE, job), JobSpec("a", MINUTE, job)], lease, clock)
    with pytest.raises(ValueError, match="interval"):
        JobSpec("a", timedelta(0), job)
