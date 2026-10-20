"""Build something slow or fallible in the background, retrying with backoff, and report why it is not ready.

The API process must start listening immediately and stay alive even when building the container fails (a missing
or invalid contracts file, Fabric unreachable during the first catalog load). The supervisor retries the build with
exponential backoff and exposes only a fixed reason code and the exception *type* to the readiness probe, which is
unauthenticated: exception messages can carry file paths and other detail, so they go to the (redacted) logs and never
into the response. Nothing here ever raises into the process, so there is no crash loop.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from orqentis.observability.logging import get_logger

_log = get_logger(__name__)

REASON_STARTING = "starting"
REASON_BUILD_FAILED = "build_failed"


@dataclass(frozen=True)
class BuildStatus:
    ready: bool
    attempts: int
    # None once ready; otherwise REASON_STARTING (no attempt has failed yet) or REASON_BUILD_FAILED.
    reason: str | None
    # Class name of the last build failure, never its message.
    error_type: str | None
    retry_in_seconds: float | None


class BuildSupervisor[T]:
    def __init__(
        self,
        build: Callable[[], Awaitable[T]],
        *,
        initial_backoff: float = 2.0,
        max_backoff: float = 60.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._build = build
        self._initial = initial_backoff
        self._max = max_backoff
        self._sleep = sleep
        self._attempts = 0
        self._reason: str | None = REASON_STARTING
        self._error_type: str | None = None
        self._retry_in: float | None = None
        self._ready = False
        self._first_attempt = asyncio.Event()

    @property
    def status(self) -> BuildStatus:
        if self._ready:
            return BuildStatus(True, self._attempts, None, None, None)
        return BuildStatus(False, self._attempts, self._reason, self._error_type, self._retry_in)

    async def wait_first_attempt(self, grace_seconds: float) -> bool:
        """Wait until the first build attempt finished (either way). ``False`` if it is still running."""
        try:
            async with asyncio.timeout(grace_seconds):
                await self._first_attempt.wait()
        except TimeoutError:
            return False
        return True

    async def run(self) -> T:
        """Try until the build succeeds. Only cancellation ends this early."""
        while True:
            self._attempts += 1
            self._retry_in = None
            try:
                built = await self._build()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._reason, self._error_type = REASON_BUILD_FAILED, type(exc).__name__
                delay = min(self._max, self._initial * 2 ** (self._attempts - 1))
                self._retry_in = delay
                _log.exception(
                    "container build failed; will retry",
                    extra={"attempt": self._attempts, "retry_in_seconds": delay},
                )
                self._first_attempt.set()
                await self._sleep(delay)
                continue
            self._ready, self._reason, self._error_type, self._retry_in = True, None, None, None
            self._first_attempt.set()
            _log.info("container built", extra={"attempts": self._attempts})
            return built
