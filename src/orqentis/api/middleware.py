"""Request guards: body size limit (413), per-principal rate limits (429 + Retry-After) and docs lockdown.

* **Body size** (pure ASGI middleware, ``BodyLimitMiddleware``). A declared ``Content-Length`` over the limit is
  refused before the app runs. Chunked or under-declared bodies are counted as the app reads them, and the
  read fails with 413 at the limit.
* **Rate limits** (``RateLimits``, consulted by the route guards in ``orqentis.api.authz``). Token bucket per
  *authenticated* principal and request class. Mutating calls (approve, reject, execute) share one budget and
  the expensive ones (telemetry collection, investigations, audit verification) have a smaller one. Plain reads
  are not limited. The limiter runs only after ``current_principal`` has authenticated the caller, so it never
  keys on an identity that was not validated. In particular, a forged ``X-MS-CLIENT-PRINCIPAL`` on a path Easy
  Auth does not authenticate (``/health``) or on an unrouted path cannot consume anyone's budget, because no
  route there reads it. Traffic without a valid identity is Easy Auth's job (401 before the app) and is not
  limited here.
* **Per replica.** State is in memory. With N replicas the effective limit is N times the setting. The MVP
  runs two or more replicas in test and prod, so the limit there is looser than the setting (a shared limiter is the
  follow-up; put Front Door or API Management in front for a global limit).
* **Docs lockdown.** Under Easy Auth the interactive docs and the OpenAPI document are removed. They would
  answer any authenticated caller, including one with no FabricOps role.

``install_api_hardening`` wires all of it with one call in ``create_app``.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from typing import Final, Literal

from fastapi import FastAPI
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from orqentis.api.errors import ApiError, api_error_handler, error_response
from orqentis.config.settings import ApiAuthMode, Settings
from orqentis.observability.logging import get_logger

_log = get_logger(__name__)

RequestClass = Literal["mutating", "expensive"]

_DOC_PATHS: Final = frozenset({"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"})
_MAX_KEY_LENGTH = 256
_MAX_BUCKETS = 10_000
_OVERFLOW_KEY = "\x00overflow"  # cannot collide with a principal id


class TokenBucketLimiter:
    """Allows ``limit_per_minute`` requests per key in a burst, refilled continuously over a minute.

    Memory is bounded without letting new keys reset a bucket that is limiting someone: when the table is
    full, only buckets that have refilled completely (idle, so dropping them loses nothing) are evicted, and if
    none can be evicted new keys share one overflow bucket instead of displacing the existing ones.
    """

    def __init__(
        self, limit_per_minute: int, *, clock: Callable[[], float] = time.monotonic, max_keys: int | None = None
    ) -> None:
        self._capacity = float(limit_per_minute)
        self._refill_per_second = limit_per_minute / 60.0
        self._clock = clock
        self._max_keys = max_keys
        self._buckets: dict[str, tuple[float, float]] = {}  # key -> (tokens, last refill)
        self._next_sweep = 0.0

    def acquire(self, key: str) -> float:
        """Take one token. Returns 0.0 when allowed, else the seconds until a token is available."""
        now = self._clock()
        if key not in self._buckets and len(self._buckets) >= (self._max_keys or _MAX_BUCKETS):
            self._sweep(now)
            if len(self._buckets) >= (self._max_keys or _MAX_BUCKETS):
                key = _OVERFLOW_KEY
        tokens, last = self._buckets.get(key, (self._capacity, now))
        tokens = min(self._capacity, tokens + (now - last) * self._refill_per_second)
        if tokens >= 1.0:
            self._buckets[key] = (tokens - 1.0, now)
            return 0.0
        self._buckets[key] = (tokens, now)
        return (1.0 - tokens) / self._refill_per_second

    def _sweep(self, now: float) -> None:
        """Drop buckets that are full again. At most once a second, so a flood does not make every request a scan."""
        if now < self._next_sweep:
            return
        self._next_sweep = now + 1.0
        for key, (tokens, last) in list(self._buckets.items()):
            if key != _OVERFLOW_KEY and tokens + (now - last) * self._refill_per_second >= self._capacity:
                del self._buckets[key]


class RateLimits:
    """The per-principal budgets of one app instance. ``check`` raises 429 when a budget is spent."""

    def __init__(self, settings: Settings, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._limiters: dict[RequestClass, TokenBucketLimiter] | None = None
        if settings.api_rate_limit_enabled:
            self._limiters = {
                "mutating": TokenBucketLimiter(settings.api_rate_limit_mutating_per_minute, clock=clock),
                "expensive": TokenBucketLimiter(settings.api_rate_limit_expensive_per_minute, clock=clock),
            }

    def check(self, request_class: RequestClass, principal_id: str) -> None:
        if self._limiters is None:
            return
        wait = self._limiters[request_class].acquire(principal_id[:_MAX_KEY_LENGTH])
        if wait <= 0.0:
            return
        retry_after = max(1, math.ceil(wait))
        _log.warning(
            "rate limit exceeded",
            extra={"request_class": request_class, "principal_id": principal_id, "retry_after_seconds": retry_after},
        )
        raise ApiError(
            429,
            "RATE_LIMITED",
            f"Too many {request_class} requests; retry in {retry_after} s",
            headers={"Retry-After": str(retry_after)},
        )


class RefusalAudit:
    """Audit refused API calls without letting a caller flood the audit log.

    A refusal is recorded at most once per principal and reason per window, and at most ``max_per_window``
    records are written per window overall. The refusals skipped in between are counted and reported in the
    next record that is written (``suppressed``). The refusal itself is never skipped: this only bounds how
    much of it is written to the hash-chained log.
    """

    def __init__(
        self,
        *,
        window_seconds: float = 60.0,
        max_per_window: int = 20,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._window = window_seconds
        self._max = max_per_window
        self._clock = clock
        self._window_start = clock()
        self._written = 0
        self._suppressed = 0
        self._seen: dict[tuple[str, str], float] = {}

    def should_record(self, principal_id: str, code: str) -> tuple[bool, int]:
        """Whether to write a record now, and how many were suppressed since the previous one."""
        now = self._clock()
        if now - self._window_start >= self._window:
            self._window_start, self._written = now, 0
            self._seen = {k: t for k, t in self._seen.items() if now - t < self._window}
        key = (principal_id[:_MAX_KEY_LENGTH], code)
        if self._written >= self._max or (key in self._seen and now - self._seen[key] < self._window):
            self._suppressed += 1
            return False, 0
        self._seen[key] = now
        self._written += 1
        suppressed, self._suppressed = self._suppressed, 0
        return True, suppressed


class BodyLimitMiddleware:
    def __init__(self, app: ASGIApp, *, max_body_bytes: int) -> None:
        self._app = app
        self._max_body = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        refusal = self._check_declared_size(Headers(scope=scope))
        if refusal is not None:
            await refusal(scope, receive, send)
            return
        await self._app(scope, self._counting(receive), send)

    def _check_declared_size(self, headers: Headers) -> JSONResponse | None:
        declared = headers.get("content-length")
        if declared is None:
            return None
        try:
            length = int(declared)
        except ValueError:
            length = -1
        if length < 0:
            return error_response(400, "BAD_REQUEST", "Invalid Content-Length header")
        if length > self._max_body:
            return error_response(413, "PAYLOAD_TOO_LARGE", f"Request body exceeds {self._max_body} bytes")
        return None

    def _counting(self, receive: Receive) -> Receive:
        received = 0

        async def counted() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self._max_body:
                    raise ApiError(413, "PAYLOAD_TOO_LARGE", f"Request body exceeds {self._max_body} bytes")
            return message

        return counted


def install_api_hardening(app: FastAPI, settings: Settings) -> None:
    """Register the body limit, the rate limits and the handler for coded API errors."""
    app.add_exception_handler(ApiError, api_error_handler)
    app.add_middleware(BodyLimitMiddleware, max_body_bytes=settings.api_max_body_bytes)
    app.state.rate_limits = RateLimits(settings)
    app.state.refusal_audit = RefusalAudit()
    if settings.api_auth_mode is ApiAuthMode.EASY_AUTH:
        app.router.routes[:] = [r for r in app.router.routes if getattr(r, "path", None) not in _DOC_PATHS]
