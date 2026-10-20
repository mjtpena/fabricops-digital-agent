"""LeaderLease contract: one holder at a time, a fencing token that only moves forward (real time, short TTLs)."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from tests.contract.conftest import LeaseFactory

TTL = timedelta(seconds=30)
SHORT = timedelta(seconds=1)
AFTER_SHORT = 1.4  # seconds to wait for SHORT to lapse


async def test_only_one_holder_at_a_time(leases: LeaseFactory) -> None:
    a, b = leases.holder("replica-a"), leases.holder("replica-b")
    granted = await a.acquire("scheduler", TTL)
    assert granted is not None and granted.holder_id == "replica-a" and granted.fencing_token >= 1
    assert await b.acquire("scheduler", TTL) is None
    again = await a.acquire("scheduler", TTL)  # re-acquiring our own lease extends it and keeps the token
    assert again is not None and again.fencing_token == granted.fencing_token


async def test_leases_are_named_independently(leases: LeaseFactory) -> None:
    a, b = leases.holder("replica-a"), leases.holder("replica-b")
    assert await a.acquire("one", TTL) is not None
    assert await b.acquire("two", TTL) is not None


async def test_an_expired_lease_is_taken_over_with_a_higher_token_and_the_old_holder_is_deposed(
    leases: LeaseFactory,
) -> None:
    a, b = leases.holder("replica-a"), leases.holder("replica-b")
    first = await a.acquire("scheduler", SHORT)
    assert first is not None
    await asyncio.sleep(AFTER_SHORT)
    taken = await b.acquire("scheduler", TTL)
    assert taken is not None and taken.holder_id == "replica-b" and taken.fencing_token == first.fencing_token + 1
    assert await a.renew(first, TTL) is None  # deposed
    await a.release(first)  # a no-op: it must not free B's lease
    assert await a.acquire("scheduler", TTL) is None


async def test_renew_extends_and_keeps_the_token_even_after_expiry_if_nobody_took_over(leases: LeaseFactory) -> None:
    a, b = leases.holder("replica-a"), leases.holder("replica-b")
    lease = await a.acquire("scheduler", SHORT)
    assert lease is not None
    renewed = await a.renew(lease, TTL)
    assert renewed is not None and renewed.fencing_token == lease.fencing_token
    assert renewed.expires_at > lease.expires_at
    assert await b.acquire("scheduler", TTL) is None  # still held
    # A slow leader nobody replaced keeps its tenure: expiry alone does not revoke the lease.
    slow = await a.acquire("slow", SHORT)
    assert slow is not None
    await asyncio.sleep(AFTER_SHORT)
    still = await a.renew(slow, TTL)
    assert still is not None and still.fencing_token == slow.fencing_token


async def test_release_frees_the_lease_and_the_token_keeps_growing(leases: LeaseFactory) -> None:
    a, b = leases.holder("replica-a"), leases.holder("replica-b")
    first = await a.acquire("scheduler", TTL)
    assert first is not None
    await a.release(first)
    assert await a.renew(first, TTL) is None  # released
    second = await b.acquire("scheduler", TTL)
    assert second is not None and second.fencing_token > first.fencing_token
    third = await a.acquire("scheduler", TTL)
    assert third is None
    await b.release(second)
    fourth = await a.acquire("scheduler", TTL)
    assert fourth is not None and fourth.fencing_token > second.fencing_token


async def test_many_contenders_for_a_free_lease_produce_exactly_one_holder(leases: LeaseFactory) -> None:
    holders = [leases.holder(f"replica-{n}") for n in range(12)]
    results = await asyncio.gather(*(h.acquire("scheduler", TTL) for h in holders))
    granted = [r for r in results if r is not None]
    assert len(granted) == 1
