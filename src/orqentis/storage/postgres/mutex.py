"""PostgreSQL :class:`~orqentis.runtime.mutex.Mutex`: a session-level advisory lock on a dedicated connection.

The lock lives and dies with its connection, so a **watchdog** checks, every second, that the connection answers and
still holds the lock (``pg_locks``). If it does not (the backend was terminated, a failover, a network drop),
another instance may already hold the lock, so the section is cancelled and raises :class:`MutexLostError` instead of
carrying on as if it were alone. The window between the loss and the cancellation is at most the check interval; what
runs in it is backed by unique keys in the database (one incident per detection).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta

from orqentis.observability.logging import get_logger
from orqentis.runtime.mutex import DEFAULT_MAX_WAIT, InProcessMutex, MutexLostError, MutexTimeoutError
from orqentis.storage.postgres.database import Connection, PostgresDatabase

_log = get_logger(__name__)

_POLL_SECONDS = 0.2
WATCHDOG_SECONDS = 1.0
_WATCHDOG_QUERY_TIMEOUT = 5.0

_HOLDS_LOCK = (
    "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND pid = pg_backend_pid() AND granted "
    "AND classid = $1::oid AND objid = $2::oid AND objsubid = 1)"
)


def lock_key(name: str) -> int:
    """A stable signed 64-bit key for a lock name (the namespace is shared with the other advisory locks)."""
    digest = hashlib.sha256(f"orqentis.mutex:{name}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


class PostgresMutex:
    def __init__(self, db: PostgresDatabase, *, watchdog_seconds: float = WATCHDOG_SECONDS) -> None:
        self._db = db
        self._watchdog_seconds = watchdog_seconds
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
            lost = asyncio.Event()
            owner = asyncio.current_task()
            watchdog = asyncio.create_task(self._watch(conn, key, name, owner, lost), name=f"mutex-watchdog:{name}")
            try:
                yield
            except asyncio.CancelledError:
                if lost.is_set():
                    raise MutexLostError(
                        f"The {name!r} lock was lost while it was held; the section was cancelled"
                    ) from None
                raise
            finally:
                watchdog.cancel()
                await asyncio.gather(watchdog, return_exceptions=True)
                if not lost.is_set():
                    with contextlib.suppress(Exception):  # a broken connection has lost the lock anyway
                        await conn.execute("SELECT pg_advisory_unlock($1)", key)

    async def _watch(
        self, conn: Connection, key: int, name: str, owner: asyncio.Task[object] | None, lost: asyncio.Event
    ) -> None:
        """Cancel the section as soon as the connection that carries the lock stops holding it."""
        unsigned = key & 0xFFFFFFFFFFFFFFFF
        class_id, object_id = unsigned >> 32, unsigned & 0xFFFFFFFF
        while True:
            await asyncio.sleep(self._watchdog_seconds)
            try:
                held = await asyncio.wait_for(conn.fetchval(_HOLDS_LOCK, class_id, object_id), _WATCHDOG_QUERY_TIMEOUT)
            except Exception as exc:  # a dead or unreachable connection: assume the lock is gone (fail closed)
                held = False
                _log.error(
                    "mutex connection failed; cancelling the section",
                    extra={"mutex": name, "error": type(exc).__name__},
                )
            if not held:
                lost.set()
                _log.error("mutex lock lost; cancelling the section", extra={"mutex": name})
                if owner is not None:
                    owner.cancel()
                return
