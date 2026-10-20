"""ReplicaCatalogSync: every replica refreshes its own memory; the leader leaves it to its scheduled job."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any, cast

import pytest

from orqentis.runtime.replica_sync import ReplicaCatalogSync
from orqentis.topology.refresh import CatalogRefresher


class FakeRefresher:
    def __init__(self) -> None:
        self.calls: list[bool] = []
        self.failures: list[BaseException] = []

    async def refresh(self, *, shared_effects: bool = True) -> None:
        self.calls.append(shared_effects)
        if self.failures:
            raise self.failures.pop(0)


def sync_for(refresher: FakeRefresher, *, leader: bool = False, sleep: Any = None) -> ReplicaCatalogSync:
    kwargs = {"sleep": sleep} if sleep is not None else {}
    return ReplicaCatalogSync(cast(CatalogRefresher, refresher), lambda: leader, timedelta(minutes=5), **kwargs)


def test_a_non_positive_interval_is_refused() -> None:
    with pytest.raises(ValueError, match="positive"):
        ReplicaCatalogSync(cast(CatalogRefresher, FakeRefresher()), lambda: False, timedelta(0))


async def test_a_follower_refreshes_its_own_memory_without_the_shared_effects() -> None:
    refresher = FakeRefresher()
    assert await sync_for(refresher).run_once()
    assert refresher.calls == [False]  # shared_effects=False: no shared table rewrite, no audit of the diff


async def test_the_leader_skips_because_its_scheduled_job_does_the_same_work() -> None:
    refresher = FakeRefresher()
    assert not await sync_for(refresher, leader=True).run_once()
    assert refresher.calls == []


async def test_a_failing_refresh_is_reported_once_and_retried_and_recovery_is_noticed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    refresher = FakeRefresher()
    refresher.failures = [RuntimeError("fabric unreachable"), RuntimeError("fabric unreachable")]
    sync = sync_for(refresher)
    with caplog.at_level("INFO"):
        assert not await sync.run_once()
        assert not await sync.run_once()
        assert await sync.run_once()
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1 and "stay in force" in warnings[0].getMessage()  # the change of health, not every retry
    assert any("recovered" in r.getMessage() for r in caplog.records)
    assert refresher.calls == [False, False, False]


async def test_a_secret_in_a_failure_is_not_logged(caplog: pytest.LogCaptureFixture) -> None:
    refresher = FakeRefresher()
    refresher.failures = [RuntimeError("Authorization: Bearer abc.def.ghi client_secret=hunter2-SECRET")]
    with caplog.at_level("WARNING"):
        await sync_for(refresher).run_once()
    assert "hunter2" not in caplog.text and "abc.def.ghi" not in caplog.text


async def test_the_loop_runs_each_interval_survives_failures_and_stops_on_request() -> None:
    refresher = FakeRefresher()
    refresher.failures = [RuntimeError("boom")]
    sleeps: list[float] = []
    ticked = asyncio.Event()

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 3:
            ticked.set()
            await asyncio.Event().wait()  # park the loop after the third interval until stop() cancels it
        await asyncio.sleep(0)

    sync = sync_for(refresher, sleep=sleep)
    await sync.start()
    await sync.start()  # idempotent: one loop
    assert sync.is_running
    await asyncio.wait_for(ticked.wait(), 5)
    await sync.stop()
    assert not sync.is_running
    assert sleeps == [300.0, 300.0, 300.0]  # the first run is one interval away: start-up just loaded everything
    assert refresher.calls == [False, False]  # the first failed, the second succeeded, the loop never stopped
