"""AuditAnchorStore contract: write-once anchors per epoch, the latest by sequence, conflicts reported."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from orqentis.observability.anchors import AnchorConflictError, AuditAnchor, AuditAnchorStore

AT = datetime(2026, 10, 20, 8, 0, tzinfo=UTC)


def anchor(sequence: int, hash_: str | None = None, epoch: int = 1) -> AuditAnchor:
    return AuditAnchor(epoch=epoch, sequence=sequence, hash=hash_ or f"{sequence:064x}", anchored_at=AT)


async def test_the_latest_anchor_of_an_epoch_is_the_highest_sequence(anchor_store: AuditAnchorStore) -> None:
    assert await anchor_store.latest(1) is None
    for sequence in (5, 40, 7, 1000):
        assert await anchor_store.put(anchor(sequence))
    latest = await anchor_store.latest(1)
    assert latest is not None and latest.sequence == 1000 and latest.hash == f"{1000:064x}"


async def test_putting_the_same_anchor_twice_is_a_no_op(anchor_store: AuditAnchorStore) -> None:
    assert await anchor_store.put(anchor(5))
    assert not await anchor_store.put(anchor(5))
    assert (await anchor_store.latest(1)) == anchor(5)


async def test_epochs_are_separate(anchor_store: AuditAnchorStore) -> None:
    await anchor_store.put(anchor(50, epoch=1))
    assert await anchor_store.latest(2) is None
    await anchor_store.put(anchor(3, epoch=2))
    latest = await anchor_store.latest(2)
    assert latest is not None and latest.sequence == 3
    first = await anchor_store.latest(1)
    assert first is not None and first.sequence == 50


async def test_two_different_hashes_for_one_sequence_are_a_conflict(anchor_store: AuditAnchorStore) -> None:
    await anchor_store.put(anchor(9, "a" * 64))
    await anchor_store.put(anchor(9, "b" * 64))
    with pytest.raises(AnchorConflictError, match="sequence 9"):
        await anchor_store.latest(1)
    await anchor_store.put(anchor(10))  # a later, unambiguous anchor supersedes the conflict
    latest = await anchor_store.latest(1)
    assert latest is not None and latest.sequence == 10
