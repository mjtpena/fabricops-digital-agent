"""A mutex whose lock connection dies cancels its holder instead of letting two sections run at once.

Reproduced against the first version: the holder kept running with no error after its backend was terminated, while a
second instance took the lock and entered the same section (``A:enter, B:enter, B:exit, A: still running``).
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import asyncpg
import pytest
from tests.support.services import PostgresServer

from orqentis.config.clock import SystemClock
from orqentis.runtime.mutex import MutexLostError
from orqentis.storage.postgres import PostgresConfig, PostgresStorage
from orqentis.storage.postgres.mutex import PostgresMutex, lock_key

pytestmark = pytest.mark.security


async def terminate_the_holder(server: PostgresServer, name: str) -> int:
    """Terminate the backend that holds the advisory lock ``name`` (a failover, an idle kill, a network drop)."""
    unsigned = lock_key(name) & 0xFFFFFFFFFFFFFFFF
    conn = await asyncpg.connect(host=server.host, port=server.port, user=server.user, database=server.database)
    try:
        pid = await conn.fetchval(
            "SELECT pid FROM pg_locks WHERE locktype = 'advisory' AND granted "
            "AND classid = $1::oid AND objid = $2::oid",
            unsigned >> 32,
            unsigned & 0xFFFFFFFF,
        )
        assert pid is not None, "nobody holds the lock"
        assert await conn.fetchval("SELECT pg_terminate_backend($1)", pid)
        return int(pid)
    finally:
        await conn.close()


async def test_a_holder_whose_lock_connection_is_terminated_is_cancelled_with_a_clear_error(
    pg_config: PostgresConfig, postgres_server: PostgresServer
) -> None:
    storage = await PostgresStorage.open(pg_config, None, SystemClock())
    fast = PostgresMutex(storage.database, watchdog_seconds=0.2)
    entered, finished = asyncio.Event(), asyncio.Event()

    async def holder() -> None:
        async with fast.hold("detection", max_wait=timedelta(seconds=5)):
            entered.set()
            await asyncio.sleep(30)  # a long detection pass
            finished.set()

    task = asyncio.create_task(holder())
    try:
        async with asyncio.timeout(10):
            await entered.wait()
        await terminate_the_holder(postgres_server, "detection")
        with pytest.raises(MutexLostError, match="lock was lost"):
            await asyncio.wait_for(task, 10)
        assert not finished.is_set()  # the section did not carry on as if it still excluded everyone
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await storage.close()


async def test_the_lock_is_free_again_after_a_loss_and_a_normal_section_still_works(
    pg_config: PostgresConfig, postgres_server: PostgresServer
) -> None:
    storage = await PostgresStorage.open(pg_config, None, SystemClock())
    mutex = PostgresMutex(storage.database, watchdog_seconds=0.2)
    entered = asyncio.Event()

    async def holder() -> None:
        async with mutex.hold("detection"):
            entered.set()
            await asyncio.sleep(30)

    task = asyncio.create_task(holder())
    try:
        async with asyncio.timeout(10):
            await entered.wait()
        await terminate_the_holder(postgres_server, "detection")
        with pytest.raises(MutexLostError):
            await asyncio.wait_for(task, 10)
        ran: list[str] = []
        async with mutex.hold("detection", max_wait=timedelta(seconds=5)):
            ran.append("second holder")
        assert ran == ["second holder"]
        # and a section that is not interrupted is untouched by the watchdog
        async with mutex.hold("detection"):
            await asyncio.sleep(0.6)  # several watchdog intervals
            ran.append("quiet section")
        assert ran == ["second holder", "quiet section"]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await storage.close()
