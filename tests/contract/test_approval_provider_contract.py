"""ApprovalProvider contract, including the compare-and-set that makes decisions and execution claims atomic."""

from __future__ import annotations

import asyncio

from tests.contract.conftest import make_approval

from orqentis.approvals.provider import ApprovalProvider
from orqentis.domain.approvals import ApprovalStatus


async def test_submit_get_list(approval_provider: ApprovalProvider) -> None:
    one, two = make_approval(1), make_approval(2, "INC-2026-0002")
    await approval_provider.submit(one)
    await approval_provider.submit(two)
    assert await approval_provider.get("APR-1") == one
    assert await approval_provider.get("APR-9") is None
    assert [a.approval_id for a in await approval_provider.list()] == ["APR-1", "APR-2"]
    assert [a.approval_id for a in await approval_provider.list(incident_id="INC-2026-0002")] == ["APR-2"]
    assert [a.approval_id for a in await approval_provider.list(status=ApprovalStatus.PENDING)] == ["APR-1", "APR-2"]
    assert await approval_provider.list(status=ApprovalStatus.APPROVED) == []


async def test_transition_stores_the_change_and_bumps_the_revision(approval_provider: ApprovalProvider) -> None:
    await approval_provider.submit(make_approval(1))
    approved = make_approval(1).model_copy(update={"status": ApprovalStatus.APPROVED, "approved_by": "olivia"})
    stored = await approval_provider.transition(approved, expected_revision=0)
    assert stored is not None and stored.revision == 1 and stored.status is ApprovalStatus.APPROVED
    assert await approval_provider.get("APR-1") == stored
    cancelled = stored.model_copy(update={"status": ApprovalStatus.CANCELLED})
    second = await approval_provider.transition(cancelled, expected_revision=1)
    assert second is not None and second.revision == 2
    assert [a.approval_id for a in await approval_provider.list(status=ApprovalStatus.CANCELLED)] == ["APR-1"]


async def test_a_stale_revision_changes_nothing(approval_provider: ApprovalProvider) -> None:
    await approval_provider.submit(make_approval(1))
    first = make_approval(1).model_copy(update={"status": ApprovalStatus.APPROVED})
    assert await approval_provider.transition(first, expected_revision=0) is not None
    rejected = make_approval(1).model_copy(update={"status": ApprovalStatus.REJECTED})
    assert await approval_provider.transition(rejected, expected_revision=0) is None
    stored = await approval_provider.get("APR-1")
    assert stored is not None and stored.status is ApprovalStatus.APPROVED and stored.revision == 1
    unknown = rejected.model_copy(update={"approval_id": "APR-404"})
    assert await approval_provider.transition(unknown, expected_revision=0) is None


async def test_of_many_racing_writers_exactly_one_wins(approval_provider: ApprovalProvider) -> None:
    await approval_provider.submit(make_approval(1))
    contenders = [
        make_approval(1).model_copy(update={"status": ApprovalStatus.APPROVED, "approved_by": f"user-{n}"})
        for n in range(20)
    ]
    results = await asyncio.gather(*(approval_provider.transition(c, expected_revision=0) for c in contenders))
    winners = [r for r in results if r is not None]
    assert len(winners) == 1
    stored = await approval_provider.get("APR-1")
    assert stored is not None and stored.revision == 1 and stored.approved_by == winners[0].approved_by


async def test_save_is_an_unconditional_upsert(approval_provider: ApprovalProvider) -> None:
    await approval_provider.save(make_approval(1))
    await approval_provider.save(make_approval(1).model_copy(update={"status": ApprovalStatus.EXPIRED, "revision": 3}))
    stored = await approval_provider.get("APR-1")
    assert stored is not None and stored.status is ApprovalStatus.EXPIRED and stored.revision == 3
    assert len(await approval_provider.list()) == 1
