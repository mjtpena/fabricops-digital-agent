"""Request guards: body size limit (413) and per-principal rate limiting (429 + Retry-After).

Both are pure ASGI middleware, registered by ``install_api_hardening`` (one line in ``create_app``).

* **Body size.** A declared ``Content-Length`` over the limit is refused before the app runs. Chunked or
  under-declared bodies are counted as the app reads them, and the read fails with 413 at the limit.
* **Rate limit.** Token bucket per principal and request class. Mutating calls (any non-safe method) share
  one budget, and the expensive ones (telemetry collection, investigations, audit verification) have a
  smaller one. Plain reads are not limited. Callers without an identity share one bucket per client address.
* **Per replica.** State is in memory. With N replicas the effective limit is N times the setting. The MVP
  runs a single replica (SQLite stores), so this is exact today.

The principal key is read from the header Easy Auth set (``X-MS-CLIENT-PRINCIPAL``) or, under
``dev_headers``, from ``X-Orqentis-User``. It is only a bucket key. Authentication still happens in
``orqentis.api.auth``.
"""

from __future__ import annotations

import math
import re
import time
from collections.abc import Callable
from typing import Final

from fastapi import FastAPI
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from orqentis.api.claims import object_id_from_header
from orqentis.api.errors import ApiError, api_error_handler, error_response
from orqentis.config.settings import ApiAuthMode, Settings
from orqentis.observability.logging import get_logger

_log = get_logger(__name__)

_SAFE_METHODS: Final = frozenset({"GET", "HEAD", "OPTIONS"})
_EXPENSIVE: Final = (
    ("POST", re.compile(r"^/telemetry/collect/?$")),
    ("POST", re.compile(r"^/incidents/[^/]+/investigate/?$")),
    ("GET", re.compile(r"^/audit/verify/?$")),
)
_MAX_KEY_LENGTH = 256
_MAX_BUCKETS = 10_000


class TokenBucketLimiter:
    """Allows ``limit_per_minute`` requests per key in a burst, refilled continuously over a minute."""

    def __init__(self, limit_per_minute: int, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._capacity = float(limit_per_minute)
        self._refill_per_second = limit_per_minute / 60.0
        self._clock = clock
        self._buckets: dict[str, tuple[float, float]] = {}  # key -> (tokens, last refill)

    def acquire(self, key: str) -> float:
        """Take one token. Returns 0.0 when allowed, else the seconds until a token is available."""
        now = self._clock()
        tokens, last = self._buckets.get(key, (self._capacity, now))
        tokens = min(self._capacity, tokens + (now - last) * self._refill_per_second)
        if tokens >= 1.0:
            self._buckets[key] = (tokens - 1.0, now)
            self._prune(now)
            return 0.0
        self._buckets[key] = (tokens, now)
        return (1.0 - tokens) / self._refill_per_second

    def _prune(self, now: float) -> None:
        if len(self._buckets) <= _MAX_BUCKETS:
            return
        # A bucket idle for a full minute is full again, so dropping it loses nothing.
        for key in [k for k, (_, last) in self._buckets.items() if now - last >= 60.0]:
            del self._buckets[key]
        # Still over the cap (a flood of distinct keys): evict the oldest entries down to 90 % so the scan above
        # is not repeated on every request. This can only forget budgets, never wrongly refuse a request.
        for key in list(self._buckets)[: max(0, len(self._buckets) - _MAX_BUCKETS * 9 // 10)]:
            del self._buckets[key]


def _request_class(method: str, path: str) -> str | None:
    for expensive_method, pattern in _EXPENSIVE:
        if method == expensive_method and pattern.match(path):
            return "expensive"
    return None if method in _SAFE_METHODS else "mutating"


def _principal_key(headers: Headers, scope: Scope, auth_mode: ApiAuthMode) -> str:
    identity: str | None
    if auth_mode is ApiAuthMode.EASY_AUTH:
        identity = object_id_from_header(headers.get("x-ms-client-principal"))
    else:
        identity = (headers.get("x-orqentis-user") or "dev-user@localhost").strip()
    if identity:
        return f"principal:{identity[:_MAX_KEY_LENGTH]}"
    client = scope.get("client")
    return f"anonymous:{client[0] if client else 'unknown'}"


class RequestGuardMiddleware:
    def __init__(self, app: ASGIApp, *, settings: Settings, clock: Callable[[], float] = time.monotonic) -> None:
        self._app = app
        self._max_body = settings.api_max_body_bytes
        self._auth_mode = settings.api_auth_mode
        self._limiters: dict[str, TokenBucketLimiter] | None = None
        if settings.api_rate_limit_enabled:
            self._limiters = {
                "mutating": TokenBucketLimiter(settings.api_rate_limit_mutating_per_minute, clock=clock),
                "expensive": TokenBucketLimiter(settings.api_rate_limit_expensive_per_minute, clock=clock),
            }

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        refusal = self._check_declared_size(headers) or self._check_rate(headers, scope)
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

    def _check_rate(self, headers: Headers, scope: Scope) -> JSONResponse | None:
        if self._limiters is None:
            return None
        request_class = _request_class(scope["method"], scope["path"])
        if request_class is None:
            return None
        key = _principal_key(headers, scope, self._auth_mode)
        wait = self._limiters[request_class].acquire(key)
        if wait <= 0.0:
            return None
        retry_after = max(1, math.ceil(wait))
        _log.warning(
            "rate limit exceeded",
            extra={"request_class": request_class, "principal_key": key, "retry_after_seconds": retry_after},
        )
        return error_response(
            429,
            "RATE_LIMITED",
            f"Too many {request_class} requests; retry in {retry_after} s",
            headers={"Retry-After": str(retry_after)},
        )

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
    """Register the request guards and the handler for coded API errors."""
    app.add_exception_handler(ApiError, api_error_handler)
    app.add_middleware(RequestGuardMiddleware, settings=settings)
