"""EpochMarkerStore contract: write-once markers, listed lowest epoch first, a different marker is a conflict."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from orqentis.observability.epochs import EpochMarker, EpochMarkerConflictError, EpochMarkerStore, current_epoch

AT = datetime(2026, 10, 20, 8, 0, tzinfo=UTC)


def marker(epoch: int, sequence: int = 10, hash_: str | None = None) -> EpochMarker:
    return EpochMarker(
        epoch=epoch,
        sequence=sequence,
        hash=hash_ or f"{sequence:064x}",
        started_at=AT,
        started_by="operator:olivia",
        reason="restored to 07:00Z from server old",
    )


async def test_markers_are_listed_lowest_epoch_first_and_the_current_epoch_is_the_highest(
    epoch_store: EpochMarkerStore,
) -> None:
    assert list(await epoch_store.all()) == [] and current_epoch(await epoch_store.all()) == 1
    for epoch in (3, 2, 5):
        assert await epoch_store.put(marker(epoch, sequence=epoch * 10))
    assert [m.epoch for m in await epoch_store.all()] == [2, 3, 5]
    assert current_epoch(await epoch_store.all()) == 5


async def test_putting_the_same_marker_twice_is_a_no_op_and_a_different_one_is_a_conflict(
    epoch_store: EpochMarkerStore,
) -> None:
    assert await epoch_store.put(marker(2))
    assert not await epoch_store.put(marker(2))
    with pytest.raises(EpochMarkerConflictError):
        await epoch_store.put(marker(2, sequence=11))
    with pytest.raises(EpochMarkerConflictError):
        await epoch_store.put(marker(2, hash_="a" * 64))
    (only,) = await epoch_store.all()
    assert only.sequence == 10  # the first marker stands


def test_epoch_one_has_no_marker_and_a_marker_names_a_real_hash() -> None:
    with pytest.raises(ValueError, match="epoch"):
        marker(1)
    with pytest.raises(ValueError, match="hash"):
        marker(2, hash_="not-a-hash")
