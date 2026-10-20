"""Detection is check-then-act, so it is mutually exclusive across instances: one failure, one incident."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

import pytest
from tests.support.postgres_app import AppStorage

from orqentis.bootstrap import Container, build_container
from orqentis.domain.incidents import IncidentStatus
from orqentis.runtime.mutex import MutexTimeoutError
from orqentis.storage.postgres import PostgresConfig, PostgresStorage


async def test_the_mutex_excludes_other_instances_until_it_is_released(
    pg: PostgresStorage, pg_config: PostgresConfig
) -> None:
    other = await PostgresStorage.open(pg_config, None, pg.audit._clock, migrate=False)
    try:
        async with pg.mutex.hold("detection"):
            with pytest.raises(MutexTimeoutError):
                async with other.mutex.hold("detection", max_wait=timedelta(seconds=0.6)):
                    pytest.fail("two instances held the same mutex")
            async with other.mutex.hold("something-else", max_wait=timedelta(seconds=0.6)):
                pass  # other names are independent
        async with other.mutex.hold("detection", max_wait=timedelta(seconds=5)):
            pass
    finally:
        await other.close()


async def test_a_pool_full_of_waiters_cannot_starve_the_holder(pg_config: PostgresConfig) -> None:
    """Waiters of one process queue in memory instead of each holding a connection while they poll."""
    from dataclasses import replace

    from orqentis.config.clock import SystemClock

    tight = await PostgresStorage.open(replace(pg_config, pool_max=3), None, SystemClock())
    try:

        async def section(n: int) -> int:
            async with tight.mutex.hold("detection", max_wait=timedelta(seconds=20)):
                await tight.events.count()  # the holder needs a connection of its own
                await asyncio.sleep(0.05)
                return n

        results = await asyncio.wait_for(asyncio.gather(*(section(n) for n in range(12))), 30)
        assert sorted(results) == list(range(12))
    finally:
        await tight.close()


@pytest.mark.security
async def test_detection_on_two_instances_at_once_opens_one_incident_for_one_failure(
    app_storage: AppStorage, tmp_path: Path
) -> None:
    for round_ in range(4):
        sub = AppStorage(
            app_storage.server,
            app_storage.azurite,
            f"{app_storage.schema}_{round_}",
            app_storage.evidence_container,
            app_storage.anchor_container,
        )
        containers: list[Container] = [
            await build_container(sub.settings(tmp_path / f"{round_}-a")),
            await build_container(sub.settings(tmp_path / f"{round_}-b")),
        ]
        try:
            outcomes = await asyncio.gather(*(c.collect_and_detect() for c in containers))
            opened = [i for _, created in outcomes for i in created]
            everything = await containers[0].incidents.repository.list()
            active = [i for i in everything if i.status is IncidentStatus.DETECTED]
            assert len(opened) == 1, f"round {round_}: {[i.incident_id for i in opened]}"
            assert len(active) == 1, f"round {round_}: {[i.incident_id for i in active]}"
            assert opened[0].incident_id == active[0].incident_id
        finally:
            for c in containers:
                await c.investigation_jobs.shutdown()
                await c.aclose()
            from tests.support.service_fixtures import drop_schema

            await drop_schema(sub.server, sub.schema)
