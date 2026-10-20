"""JobStore contract: idempotent submission, exclusive claims, claim numbers, expiry and reclaiming."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from orqentis.domain.incidents import IncidentStatus
from orqentis.domain.principals import SYSTEM_PRINCIPAL, Principal, PrincipalKind
from orqentis.runtime.job_store import (
    InvestigationJob,
    InvestigationJobStatus,
    InvestigationOutcome,
    InvestigationTrigger,
    JobStore,
)

OPERATOR = Principal(
    id="olivia@contoso.com", display_name="Olivia", kind=PrincipalKind.HUMAN, roles=frozenset({"FabricOps.Operator"})
)
TTL = timedelta(seconds=30)


def job(n: int, incident: str | None = None) -> InvestigationJob:
    return InvestigationJob(
        job_id=f"INV-{n}",
        incident_id=incident or f"INC-2026-{n:04d}",
        status=InvestigationJobStatus.QUEUED,
        trigger=InvestigationTrigger.API,
        requested_by=OPERATOR.id,
        created_at=datetime(2026, 10, 20, 8, n, tzinfo=UTC),
    )


def outcome() -> InvestigationOutcome:
    return InvestigationOutcome(
        incident_status=IncidentStatus.AWAITING_APPROVAL,
        trace_id="trace-1",
        findings=2,
        evidence=5,
        tool_calls=7,
        approval_ids=["APR-1"],
        confidence_level="HIGH",
        confidence_score=0.9,
    )


def now() -> datetime:
    return datetime.now(UTC)


async def test_enqueue_is_idempotent_per_incident_while_the_job_is_active(job_store: JobStore) -> None:
    first, created = await job_store.enqueue(job(1), OPERATOR)
    assert created and first.status is InvestigationJobStatus.QUEUED
    same, created_again = await job_store.enqueue(job(2, incident="INC-2026-0001"), SYSTEM_PRINCIPAL)
    assert not created_again and same.job_id == "INV-1"
    claimed = await job_store.claim_next("w1", TTL, now())
    assert claimed is not None
    still, created_third = await job_store.enqueue(job(3, incident="INC-2026-0001"), OPERATOR)
    assert not created_third and still.job_id == "INV-1"  # running counts as active too
    assert await job_store.finish(
        "INV-1", "w1", claimed.claim, status=InvestigationJobStatus.SUCCEEDED, finished_at=now(), result=outcome()
    )
    later, created_fourth = await job_store.enqueue(job(4, incident="INC-2026-0001"), OPERATOR)
    assert created_fourth and later.job_id == "INV-4"  # a finished job does not block a new one


async def test_racing_submissions_for_one_incident_create_exactly_one_job(job_store: JobStore) -> None:
    results = await asyncio.gather(
        *(job_store.enqueue(job(n, incident="INC-2026-0007"), OPERATOR) for n in range(1, 13))
    )
    assert sum(1 for _, created in results if created) == 1
    assert len({j.job_id for j, _ in results}) == 1


async def test_queries(job_store: JobStore) -> None:
    await job_store.enqueue(job(1), OPERATOR)
    await job_store.enqueue(job(2), OPERATOR)
    assert (await job_store.get("INV-1")) is not None and (await job_store.get("INV-404")) is None
    latest = await job_store.latest_for("INC-2026-0002")
    assert latest is not None and latest.job_id == "INV-2"
    assert await job_store.latest_for("INC-9999-0001") is None
    assert await job_store.active_incident_ids() == {"INC-2026-0001", "INC-2026-0002"}
    assert await job_store.is_active("INC-2026-0001") and not await job_store.is_active("INC-9999-0001")


async def test_claims_are_exclusive_fifo_and_carry_the_submitting_principal(job_store: JobStore) -> None:
    await job_store.enqueue(job(1), OPERATOR)
    await job_store.enqueue(job(2), SYSTEM_PRINCIPAL)
    one = await job_store.claim_next("w1", TTL, now())
    two = await job_store.claim_next("w2", TTL, now())
    assert one is not None and two is not None and await job_store.claim_next("w3", TTL, now()) is None
    assert (one.job.job_id, two.job.job_id) == ("INV-1", "INV-2")
    assert one.job.status is InvestigationJobStatus.RUNNING and one.job.started_at is not None
    assert one.principal == OPERATOR and two.principal == SYSTEM_PRINCIPAL
    assert not one.recovered
    running = await job_store.get("INV-1")
    assert running is not None and running.status is InvestigationJobStatus.RUNNING


async def test_many_workers_claiming_at_once_never_get_the_same_job(job_store: JobStore) -> None:
    for n in range(1, 7):
        await job_store.enqueue(job(n), OPERATOR)
    claims = await asyncio.gather(*(job_store.claim_next(f"w{n}", TTL, now()) for n in range(20)))
    got = [c.job.job_id for c in claims if c is not None]
    assert sorted(got) == [f"INV-{n}" for n in range(1, 7)]


async def test_only_the_current_claim_can_heartbeat_finish_or_release(job_store: JobStore) -> None:
    await job_store.enqueue(job(1), OPERATOR)
    claimed = await job_store.claim_next("w1", TTL, now())
    assert claimed is not None
    assert await job_store.heartbeat("INV-1", "w1", claimed.claim, TTL)
    assert not await job_store.heartbeat("INV-1", "w2", claimed.claim, TTL)  # someone else
    assert not await job_store.heartbeat("INV-1", "w1", claimed.claim + 1, TTL)  # a claim number it never had
    assert not await job_store.finish(
        "INV-1", "w2", claimed.claim, status=InvestigationJobStatus.FAILED, finished_at=now()
    )
    assert not await job_store.release("INV-1", "w1", claimed.claim + 1)
    assert (await job_store.get("INV-1")).status is InvestigationJobStatus.RUNNING  # type: ignore[union-attr]
    assert await job_store.finish(
        "INV-1", "w1", claimed.claim, status=InvestigationJobStatus.SUCCEEDED, finished_at=now(), result=outcome()
    )
    done = await job_store.get("INV-1")
    assert done is not None and done.status is InvestigationJobStatus.SUCCEEDED and done.result == outcome()
    assert done.finished_at is not None
    assert not await job_store.finish(
        "INV-1", "w1", claimed.claim, status=InvestigationJobStatus.FAILED, finished_at=now()
    )
    assert not await job_store.heartbeat("INV-1", "w1", claimed.claim, TTL)  # finished


async def test_release_hands_the_job_back_without_counting_an_attempt(job_store: JobStore) -> None:
    await job_store.enqueue(job(1), OPERATOR)
    claimed = await job_store.claim_next("w1", TTL, now())
    assert claimed is not None
    assert await job_store.release("INV-1", "w1", claimed.claim)
    back = await job_store.get("INV-1")
    assert back is not None and back.status is InvestigationJobStatus.QUEUED and back.started_at is None
    again = await job_store.claim_next("w2", TTL, now())
    assert again is not None and again.job.job_id == "INV-1" and again.claim > claimed.claim and not again.recovered
    # w1 is stale now: it cannot finish what w2 has claimed.
    assert not await job_store.finish(
        "INV-1", "w1", claimed.claim, status=InvestigationJobStatus.FAILED, finished_at=now()
    )
    assert await job_store.count_failed_attempts("INC-2026-0001") == 0


async def test_failed_attempts_count_until_the_next_success_and_cancellations_do_not_count(
    job_store: JobStore,
) -> None:
    incident = "INC-2026-0001"
    for n, (status, counts) in enumerate(
        [
            (InvestigationJobStatus.FAILED, True),
            (InvestigationJobStatus.FAILED, True),
            (InvestigationJobStatus.FAILED, False),  # cancelled by a shutdown
        ],
        start=1,
    ):
        await job_store.enqueue(job(n, incident=incident), OPERATOR)
        claimed = await job_store.claim_next("w1", TTL, now())
        assert claimed is not None
        await job_store.finish(
            f"INV-{n}", "w1", claimed.claim, status=status, finished_at=now(), error="boom", counts_attempt=counts
        )
        await asyncio.sleep(0.01)
    assert await job_store.count_failed_attempts(incident) == 2
    await job_store.enqueue(job(9, incident=incident), OPERATOR)
    claimed = await job_store.claim_next("w1", TTL, now())
    assert claimed is not None
    await job_store.finish("INV-9", "w1", claimed.claim, status=InvestigationJobStatus.SUCCEEDED, finished_at=now())
    assert await job_store.count_failed_attempts(incident) == 0


async def test_an_expired_claim_is_reclaimed_and_a_job_that_keeps_killing_its_workers_is_failed(
    job_store: JobStore,
) -> None:
    await job_store.enqueue(job(1), OPERATOR)
    short = timedelta(milliseconds=500)
    first = await job_store.claim_next("w1", short, now())
    assert first is not None
    assert await job_store.reclaim_expired(max_attempts=3, finished_at=now()) == []  # not expired yet
    await asyncio.sleep(0.9)
    assert await job_store.reclaim_expired(max_attempts=3, finished_at=now()) == []  # re-queued, not failed
    queued = await job_store.get("INV-1")
    assert queued is not None and queued.status is InvestigationJobStatus.QUEUED
    second = await job_store.claim_next("w2", short, now())
    assert second is not None and second.recovered and second.claim > first.claim
    # The first worker woke up: its claim is gone.
    assert not await job_store.heartbeat("INV-1", "w1", first.claim, TTL)
    assert not await job_store.finish(
        "INV-1", "w1", first.claim, status=InvestigationJobStatus.SUCCEEDED, finished_at=now()
    )
    await asyncio.sleep(0.9)
    await job_store.reclaim_expired(max_attempts=3, finished_at=now())
    third = await job_store.claim_next("w3", short, now())
    assert third is not None
    await asyncio.sleep(0.9)
    failed = await job_store.reclaim_expired(max_attempts=3, finished_at=now())  # the third abandoned claim
    assert [j.job_id for j in failed] == ["INV-1"] and failed[0].status is InvestigationJobStatus.FAILED
    assert failed[0].error is not None and "Abandoned" in failed[0].error
    assert (
        not await job_store.is_active("INC-2026-0001") and await job_store.count_failed_attempts("INC-2026-0001") == 1
    )


async def test_a_heartbeat_keeps_a_claim_alive(job_store: JobStore) -> None:
    await job_store.enqueue(job(1), OPERATOR)
    short = timedelta(milliseconds=600)
    claimed = await job_store.claim_next("w1", short, now())
    assert claimed is not None
    for _ in range(4):
        await asyncio.sleep(0.25)
        assert await job_store.heartbeat("INV-1", "w1", claimed.claim, short)
    assert await job_store.reclaim_expired(max_attempts=3, finished_at=now()) == []
    running = await job_store.get("INV-1")
    assert running is not None and running.status is InvestigationJobStatus.RUNNING


async def test_cancel_queued_fails_queued_jobs_without_counting(job_store: JobStore) -> None:
    await job_store.enqueue(job(1), OPERATOR)
    await job_store.enqueue(job(2), OPERATOR)
    running = await job_store.claim_next("w1", TTL, now())
    assert running is not None and running.job.job_id == "INV-1"
    cancelled = await job_store.cancel_queued(error="Cancelled: shutting down", finished_at=now())
    assert [j.job_id for j in cancelled] == ["INV-2"]
    gone = await job_store.get("INV-2")
    assert (
        gone is not None and gone.status is InvestigationJobStatus.FAILED and gone.error == "Cancelled: shutting down"
    )
    assert await job_store.count_failed_attempts("INC-2026-0002") == 0
    assert (await job_store.get("INV-1")).status is InvestigationJobStatus.RUNNING  # type: ignore[union-attr]
