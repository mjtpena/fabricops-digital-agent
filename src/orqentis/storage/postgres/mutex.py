"""PostgreSQL :class:`~orqentis.runtime.mutex.Mutex`: a session-level advisory lock on a dedicated connection."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta

from orqentis.runtime.mutex import DEFAULT_MAX_WAIT, InProcessMutex, MutexTimeoutError
from orqentis.storage.postgres.database import PostgresDatabase

_POLL_SECONDS = 0.2


def lock_key(name: str) -> int:
    """A stable signed 64-bit key for a lock name (the namespace is shared with the other advisory locks)."""
    digest = hashlib.sha256(f"orqentis.mutex:{name}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


class PostgresMutex:
    def __init__(self, db: PostgresDatabase) -> None:
        self._db = db
        # Tasks of this process queue here first, so at most one of them (per name) polls the database lock. Without it,
        # as many waiters as the pool has connections would each hold a connection while waiting, and the holder
        # of the lock would be unable to get one for its own work.
        self._local = InProcessMutex()

    @asynccontextmanager
    async def hold(self, name: str, *, max_wait: timedelta = DEFAULT_MAX_WAIT) -> AsyncIterator[None]:
        key = lock_key(name)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max_wait.total_seconds()
        # One connection carries the lock for the whole section. If the process dies, the connection drops and
        # PostgreSQL releases the lock; the pool also runs pg_advisory_unlock_all() when a connection is returned.
        async with self._local.hold(name, max_wait=max_wait), self._db.connection() as conn:
            while not await conn.fetchval("SELECT pg_try_advisory_lock($1)", key):
                if loop.time() >= deadline:
                    raise MutexTimeoutError(f"Could not take the {name!r} lock within {max_wait}")
                await asyncio.sleep(_POLL_SECONDS)
            try:
                yield
            finally:
                with contextlib.suppress(Exception):  # a broken connection has lost the lock anyway
                    await conn.execute("SELECT pg_advisory_unlock($1)", key)
