"""A worker that loses its claim stops, and cannot change anything afterwards: one investigation per job at a time.

Reproduced against the first version: when a worker's heartbeats kept failing it retried for ever without stopping its
investigation, and once the claim lifetime had passed another replica re-queued and ran the same job, so the incident
was investigated twice at once and the first run's writes (incident, approval requests) still landed.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from tests.conftest import detect
from tests.support.polling import poll, wait_until
from tests.support.postgres_app import AppStorage

from orqentis.agents.models import EvidencePackage, ReasonerOutput
from orqentis.agents.rule_based import RuleBasedReasoner
from orqentis.agents.toolbox import AgentToolbox
from orqentis.bootstrap import Container, build_container
from orqentis.config.clock import SystemClock
from orqentis.domain.principals import SYSTEM_PRINCIPAL
from orqentis.runtime.fencing import ClaimFence, FencingError, claim_fenced
from orqentis.runtime.investigations import InvestigationJobRunner
from orqentis.runtime.job_store import InvestigationJob, InvestigationJobStatus, InvestigationTrigger
from orqentis.storage.postgres import PostgresConfig, PostgresStorage

pytestmark = pytest.mark.security


class OverlapReasoner:
    """The rule-based reasoner, held until released; counts how many diagnoses run at the same time."""

    name = "overlap"

    def __init__(self) -> None:
        self._inner = RuleBasedReasoner(timezone="Australia/Sydney")
        self.running = 0
        self.max_running = 0
        self.started = 0
        self.release = asyncio.Event()

    async def diagnose(self, package: EvidencePackage, *, tools: AgentToolbox) -> ReasonerOutput:
        self.running += 1
        self.started += 1
        self.max_running = max(self.max_running, self.running)
        try:
            await self.release.wait()
            return await self._inner.diagnose(package, tools=tools)
        finally:
            self.running -= 1


class FlakyHeartbeats:
    """A job store whose heartbeats can be made to fail: this worker's view of a database it cannot reach."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.broken = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def heartbeat(self, *args: Any, **kwargs: Any) -> bool:
        if self.broken:
            raise ConnectionError("the database is unreachable from this worker")
        return bool(await self._inner.heartbeat(*args, **kwargs))


async def test_a_worker_whose_heartbeats_keep_failing_stops_its_investigation_before_another_replica_starts_it(
    app_storage: AppStorage, tmp_path: Path
) -> None:
    """Two replicas: separate containers share nothing in memory (no in-process lock, no common job store object), only
    the database, and one reasoner counts the diagnoses that run at the same time across both. Without the stop rule
    the first replica carries on while the second takes the job over, and ``max_running`` reaches 2."""
    reasoner = OverlapReasoner()
    container_a = await build_container(app_storage.settings(tmp_path / "a"), reasoner=reasoner)
    container_b = await build_container(app_storage.settings(tmp_path / "b"), reasoner=reasoner)
    flaky = FlakyHeartbeats(container_a.investigation_jobs._store)

    def runner(container: Container, store: Any) -> InvestigationJobRunner:
        return InvestigationJobRunner(
            container.investigations,
            container.incidents,
            container.audit,
            container.clock,
            store=store,
            max_concurrency=1,
            claim_ttl=timedelta(seconds=3),
            poll_interval=timedelta(seconds=1),
        )

    worker_a = runner(container_a, flaky)
    worker_b = runner(container_b, container_b.investigation_jobs._store)
    try:
        incident = await detect(container_a)
        await worker_a.start()
        submission = await worker_a.submit(
            incident.incident_id, requested_by=SYSTEM_PRINCIPAL, trigger=InvestigationTrigger.API
        )
        await wait_until(lambda: reasoner.running >= 1, within=30)
        flaky.broken = True  # A can no longer renew its claim...
        await worker_b.start()  # ...and B, in another process, polls for work, as a second replica does

        # A stops by itself once the claim could have been taken over; then B takes the job and runs it.
        await wait_until(lambda: reasoner.started >= 2, within=60)
        assert reasoner.max_running == 1, "the same job ran on two replicas at once"

        reasoner.release.set()
        job = await poll(
            lambda: worker_b.get(submission.job.job_id),
            lambda j: j is not None and not j.status.is_active,
            within=60,
            interval=0.2,
        )
        assert job is not None and job.status is InvestigationJobStatus.SUCCEEDED, job
        starts = [r for r in await container_b.audit.list() if r.action == "investigation.start"]
        assert len(starts) == 2 and reasoner.max_running == 1  # A's aborted run and B's, one after the other
    finally:
        reasoner.release.set()
        flaky.broken = False
        await worker_a.shutdown()
        await worker_b.shutdown()
        for container in (container_a, container_b):
            await container.investigation_jobs.shutdown()
            await container.aclose()


def job(n: int = 1) -> InvestigationJob:
    return InvestigationJob(
        job_id=f"INV-{n}",
        incident_id=f"INC-2026-{n:04d}",
        status=InvestigationJobStatus.QUEUED,
        trigger=InvestigationTrigger.API,
        requested_by=SYSTEM_PRINCIPAL.id,
        created_at=datetime(2026, 10, 20, 8, n, tzinfo=UTC),
    )


async def test_writes_made_under_a_claim_are_refused_once_another_worker_holds_the_job(pg: PostgresStorage) -> None:
    await pg.jobs.enqueue(job(), SYSTEM_PRINCIPAL)
    first = await pg.jobs.claim_next("worker-a", timedelta(seconds=30), datetime.now(UTC))
    assert first is not None
    fence = ClaimFence("INV-1", "worker-a", first.claim)

    with claim_fenced(fence):
        async with pg.database.transaction() as conn:  # the claim is ours: the write is allowed
            await conn.execute("SELECT 1")
        async with pg.database.transaction(fenced=False) as conn:  # audit records are never fenced
            await conn.execute("SELECT 1")

    # worker-a stalls; its claim expires; worker-b takes the job over.
    await pg.database.execute("UPDATE investigation_jobs SET claim_expires_at = now() - interval '1 minute'")
    assert await pg.jobs.reclaim_expired(max_attempts=3, finished_at=datetime.now(UTC)) == []
    second = await pg.jobs.claim_next("worker-b", timedelta(seconds=30), datetime.now(UTC))
    assert second is not None and second.claim > first.claim

    with claim_fenced(fence):
        with pytest.raises(FencingError, match="no longer held"):
            async with pg.database.transaction():
                pytest.fail("a stale worker's write must be refused before it runs")
        async with pg.database.transaction(fenced=False) as conn:
            await conn.execute("SELECT 1")  # its audit trail of what it did is still kept
    with claim_fenced(ClaimFence("INV-1", "worker-b", second.claim)):
        async with pg.database.transaction() as conn:
            await conn.execute("SELECT 1")  # the new holder writes freely


async def test_heartbeats_are_not_starved_by_a_stream_of_writes_made_under_the_claim(
    pg: PostgresStorage, pg_config: PostgresConfig
) -> None:
    """Reproduced: with six or more overlapping writers a heartbeat waited for ever behind row-level share locks."""
    busy = await PostgresStorage.open(replace(pg_config, pool_max=40), None, SystemClock(), migrate=False)
    await pg.jobs.enqueue(job(), SYSTEM_PRINCIPAL)
    claimed = await pg.jobs.claim_next("worker-a", timedelta(seconds=60), datetime.now(UTC))
    assert claimed is not None
    fence = ClaimFence("INV-1", "worker-a", claimed.claim)
    stop = asyncio.Event()

    async def writer() -> None:
        with claim_fenced(fence):
            while not stop.is_set():
                async with busy.database.transaction() as conn:
                    await conn.execute("SELECT pg_sleep(0.2)")

    writers = [asyncio.create_task(writer()) for _ in range(30)]
    loop = asyncio.get_running_loop()
    try:
        await asyncio.sleep(0.3)  # the writers are saturating the claim
        latencies: list[float] = []
        for _ in range(8):
            started = loop.time()
            assert await asyncio.wait_for(
                pg.jobs.heartbeat("INV-1", "worker-a", claimed.claim, timedelta(seconds=60)), 20
            )
            latencies.append(loop.time() - started)
            await asyncio.sleep(0.1)
        assert max(latencies) < 1.0, latencies
    finally:
        stop.set()
        await asyncio.gather(*writers, return_exceptions=True)
        await busy.close()


async def test_a_job_with_a_write_in_flight_is_not_taken_over_until_the_write_has_ended(pg: PostgresStorage) -> None:
    await pg.jobs.enqueue(job(), SYSTEM_PRINCIPAL)
    claimed = await pg.jobs.claim_next("worker-a", timedelta(seconds=30), datetime.now(UTC))
    assert claimed is not None
    fence = ClaimFence("INV-1", "worker-a", claimed.claim)
    entered, leave = asyncio.Event(), asyncio.Event()

    async def slow_write() -> None:
        with claim_fenced(fence):
            async with pg.database.transaction():
                entered.set()
                await leave.wait()

    # The worker stalled (its claim has expired), but a write it started earlier is still in flight.
    await pg.database.execute("UPDATE investigation_jobs SET claim_expires_at = now() - interval '1 minute'")
    writer = asyncio.create_task(slow_write())
    try:
        await asyncio.wait_for(entered.wait(), 10)
        await pg.jobs.reclaim_expired(max_attempts=3, finished_at=datetime.now(UTC))
        held = await pg.jobs.get("INV-1")
        assert held is not None and held.status is InvestigationJobStatus.RUNNING  # the write finishes first
    finally:
        leave.set()
        await writer
    await pg.jobs.reclaim_expired(max_attempts=3, finished_at=datetime.now(UTC))
    taken = await pg.jobs.get("INV-1")
    assert taken is not None and taken.status is InvestigationJobStatus.QUEUED
