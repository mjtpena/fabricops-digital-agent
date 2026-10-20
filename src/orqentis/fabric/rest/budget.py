"""Global budget for Fabric and Power BI calls: a concurrency limit plus a request-rate limit.

Every collector, tool call and catalog refresh shares one :class:`FabricCallBudget` through
:class:`~orqentis.fabric.rest.http.FabricHttp`, so a burst of autonomous work cannot exhaust the (undocumented,
per-identity) Fabric throttling limits. It cooperates with the retry logic: a ``429`` / ``Retry-After`` pauses *all*
callers (:meth:`FabricCallBudget.penalize`), not just the request that was throttled, and retry sleeps never hold a
concurrency slot.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager


class FabricCallBudget:
    """Semaphore (``max_concurrency`` calls in flight) + token bucket (``rate_per_second``, ``burst`` capacity)."""

    def __init__(
        self,
        *,
        max_concurrency: int = 4,
        rate_per_second: float = 5.0,
        burst: int = 10,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be >= 1")
        if rate_per_second <= 0:
            raise ValueError("rate_per_second must be > 0")
        if burst < 1:
            raise ValueError("burst must be >= 1")
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._max_concurrency = max_concurrency
        self._rate = rate_per_second
        self._burst = float(burst)
        self._monotonic = monotonic
        self._sleep = sleep
        self._tokens = float(burst)
        self._updated = monotonic()
        self._blocked_until = 0.0
        self._in_flight = 0
        self._peak_in_flight = 0

    @property
    def max_concurrency(self) -> int:
        return self._max_concurrency

    @property
    def in_flight(self) -> int:
        return self._in_flight

    @property
    def peak_in_flight(self) -> int:
        """Highest number of simultaneous calls observed (diagnostics and tests)."""
        return self._peak_in_flight

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        """Hold a concurrency slot and spend one rate token for the duration of one HTTP attempt."""
        await self._semaphore.acquire()
        try:
            await self._take_token()
            self._in_flight += 1
            self._peak_in_flight = max(self._peak_in_flight, self._in_flight)
            try:
                yield
            finally:
                self._in_flight -= 1
        finally:
            self._semaphore.release()

    def penalize(self, seconds: float) -> None:
        """The service asked every caller to back off: nobody starts a call before ``seconds`` have passed."""
        if seconds <= 0:
            return
        self._blocked_until = max(self._blocked_until, self._monotonic() + seconds)
        # Resume at the steady rate rather than with a full burst: nothing accrues while blocked.
        self._tokens = 0.0
        self._updated = self._blocked_until

    async def _take_token(self) -> None:
        while True:
            wait = self._reserve()
            if wait <= 0:
                return
            await self._sleep(wait)

    def _reserve(self) -> float:
        """Spend a token and return 0, or return how long to wait. Has no awaits, so it is atomic per event loop."""
        now = self._monotonic()
        if now < self._blocked_until:
            return self._blocked_until - now
        self._tokens = min(self._burst, self._tokens + (now - self._updated) * self._rate)
        self._updated = now
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return 0.0
        return (1.0 - self._tokens) / self._rate
