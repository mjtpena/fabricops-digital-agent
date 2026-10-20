"""Named mutual exclusion across the instances that share a system of record.

Some steps are "check, then act" and cannot be made safe with a single conditional write: detection looks for an
incident that already covers a failure and opens one when there is none. Two instances doing that at the same moment
(the leader's scheduled run and a ``POST /telemetry/collect`` that landed on another replica) would both find
nothing and both open an incident. A :class:`Mutex` serialises such a section across instances.

* :class:`InProcessMutex` serialises inside one process (local storage, which belongs to one process anyway).
* The PostgreSQL adapter uses a session-level advisory lock held on one connection: it is released when the section
  ends and, if the instance dies in the middle, when its connection drops, so a crash can never leave it held.

A wait is bounded: if the lock cannot be taken within ``max_wait``, :class:`MutexTimeoutError` is raised and the caller
(a scheduled job, an API request) fails visibly instead of queueing behind a stuck holder forever.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import timedelta
from typing import Protocol

DEFAULT_MAX_WAIT = timedelta(minutes=2)


class MutexTimeoutError(Exception):
    pass


class Mutex(Protocol):
    def hold(self, name: str, *, max_wait: timedelta = DEFAULT_MAX_WAIT) -> AbstractAsyncContextManager[None]:
        """Hold the named mutex for the duration of the ``async with`` block."""
        ...


class InProcessMutex:
    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}

    @asynccontextmanager
    async def hold(self, name: str, *, max_wait: timedelta = DEFAULT_MAX_WAIT) -> AsyncIterator[None]:
        lock = self._locks.setdefault(name, asyncio.Lock())
        try:
            await asyncio.wait_for(lock.acquire(), max_wait.total_seconds())
        except TimeoutError as exc:
            raise MutexTimeoutError(f"Could not take the {name!r} lock within {max_wait}") from exc
        try:
            yield
        finally:
            lock.release()
