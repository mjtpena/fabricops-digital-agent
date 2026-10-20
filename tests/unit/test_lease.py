"""Leader lease: one holder at a time, fencing tokens that only move forward."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from orqentis.config.clock import FixedClock
from orqentis.runtime.lease import InProcessLeaderLease, LeaseTable

T0 = datetime(2026, 10, 20, 8, 0, tzinfo=UTC)
TTL = timedelta(seconds=60)


def two_holders() -> tuple[FixedClock, InProcessLeaderLease, InProcessLeaderLease]:
    clock = FixedClock(T0)
    table = LeaseTable()
    return (
        clock,
        InProcessLeaderLease(clock, holder_id="replica-a", table=table),
        InProcessLeaderLease(clock, holder_id="replica-b", table=table),
    )


async def test_only_one_holder_at_a_time() -> None:
    _, a, b = two_holders()
    granted = await a.acquire("scheduler", TTL)
    assert granted is not None and granted.holder_id == "replica-a" and granted.fencing_token == 1
    assert await b.acquire("scheduler", TTL) is None
    # Re-acquiring our own lease extends it and keeps the token.
    again = await a.acquire("scheduler", TTL)
    assert again is not None and again.fencing_token == 1


async def test_leases_are_named_independently() -> None:
    _, a, b = two_holders()
    assert await a.acquire("one", TTL) is not None
    assert await b.acquire("two", TTL) is not None


async def test_an_expired_lease_can_be_taken_and_the_fencing_token_moves_forward() -> None:
    clock, a, b = two_holders()
    first = await a.acquire("scheduler", TTL)
    assert first is not None
    clock.advance(TTL + timedelta(seconds=1))
    taken = await b.acquire("scheduler", TTL)
    assert taken is not None and taken.holder_id == "replica-b" and taken.fencing_token == first.fencing_token + 1
    # The deposed holder can no longer renew, and releasing is a no-op that must not free B's lease.
    assert await a.renew(first, TTL) is None
    await a.release(first)
    assert await a.acquire("scheduler", TTL) is None


async def test_renew_extends_the_expiry_and_keeps_the_token() -> None:
    clock, a, _ = two_holders()
    lease = await a.acquire("scheduler", TTL)
    assert lease is not None
    clock.advance(timedelta(seconds=40))
    renewed = await a.renew(lease, TTL)
    assert renewed is not None
    assert renewed.fencing_token == lease.fencing_token
    assert renewed.expires_at == clock.now() + TTL


async def test_a_slow_leader_keeps_its_lease_until_someone_else_claims_it() -> None:
    clock, a, b = two_holders()
    lease = await a.acquire("scheduler", TTL)
    assert lease is not None
    clock.advance(TTL * 3)  # expired, but nobody claimed it
    renewed = await a.renew(lease, TTL)
    assert renewed is not None and renewed.fencing_token == lease.fencing_token
    assert await b.acquire("scheduler", TTL) is None  # and the renewal really took effect


async def test_release_frees_the_lease_for_another_holder() -> None:
    _, a, b = two_holders()
    lease = await a.acquire("scheduler", TTL)
    assert lease is not None
    await a.release(lease)
    taken = await b.acquire("scheduler", TTL)
    assert taken is not None and taken.fencing_token == lease.fencing_token + 1
    assert await a.renew(lease, TTL) is None
