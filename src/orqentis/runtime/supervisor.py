"""Build something slow or fallible in the background, retrying with backoff, and report why it is not ready.

The API process must start listening immediately and stay alive even when building the container fails (a missing
or invalid contracts file, Fabric unreachable during the first catalog load). The supervisor retries the build with
exponential backoff and exposes a *redacted* reason for the readiness probe; nothing here ever raises into the
process, so there is no crash loop.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from orqentis.observability.logging import get_logger
from orqentis.observability.redaction import redact_text

_log = get_logger(__name__)

_MAX_REASON_TEXT = 200


def describe_failure(exc: BaseException) -> str:
    """One short line safe to show on a readiness probe: exception type plus a redacted first line of its message."""
    first_line = (str(exc).strip().splitlines() or [""])[0]
    text = redact_text(first_line)[:_MAX_REASON_TEXT]
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


@dataclass(frozen=True)
class BuildStatus:
    ready: bool
    attempts: int
    reason: str | None
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
        self._reason: str | None = "starting"
        self._retry_in: float | None = None
        self._ready = False
        self._first_attempt = asyncio.Event()

    @property
    def status(self) -> BuildStatus:
        return BuildStatus(self._ready, self._attempts, None if self._ready else self._reason, self._retry_in)

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
                self._reason = describe_failure(exc)
                delay = min(self._max, self._initial * 2 ** (self._attempts - 1))
                self._retry_in = delay
                _log.exception(
                    "container build failed; will retry",
                    extra={"attempt": self._attempts, "retry_in_seconds": delay},
                )
                self._first_attempt.set()
                await self._sleep(delay)
                continue
            self._ready, self._reason, self._retry_in = True, None, None
            self._first_attempt.set()
            _log.info("container built", extra={"attempts": self._attempts})
            return built
