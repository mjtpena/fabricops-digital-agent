"""Bounded polling for tests whose subject runs on another task, thread or event loop."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable


async def wait_until(condition: Callable[[], bool], *, within: float = 20.0, interval: float = 0.01) -> None:
    """Return once ``condition()`` is true; fail the test after ``within`` seconds instead of hanging."""
    async with asyncio.timeout(within):
        while not condition():  # noqa: ASYNC110 - the condition is owned by another thread or event loop
            await asyncio.sleep(interval)


async def poll[T](
    fetch: Callable[[], Awaitable[T]], done: Callable[[T], bool], *, within: float = 30.0, interval: float = 0.01
) -> T:
    """Call ``fetch`` until ``done(result)``; return that result. Fails the test after ``within`` seconds."""
    async with asyncio.timeout(within):
        while True:
            result = await fetch()
            if done(result):
                return result
            await asyncio.sleep(interval)
