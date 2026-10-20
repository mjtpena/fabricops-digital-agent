"""Retrying, correlated HTTP transport for the Fabric and Power BI REST APIs.

Each logical request carries a fresh ``x-ms-client-request-id`` that stays the same across its
retries, and the service's own request id is captured from the response, so any failure can be
traced end to end (and handed to Microsoft support). Bearer tokens, Authorization headers and query
strings (which may carry continuation tokens) are never logged or copied into errors.
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from orqentis import __version__
from orqentis.fabric.errors import (
    FabricAuthError,
    FabricError,
    FabricNotFoundError,
    FabricRateLimitedError,
    FabricUnavailableError,
)
from orqentis.fabric.rest.auth import TokenProvider
from orqentis.observability.logging import get_logger
from orqentis.observability.redaction import redact, redact_text

_log = get_logger(__name__)

CLIENT_REQUEST_ID_HEADER = "x-ms-client-request-id"
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
_RESPONSE_REQUEST_ID_HEADERS = ("RequestId", "x-ms-request-id")
_MAX_ERROR_TEXT = 1000

QueryParams = Mapping[str, str | int]
JsonObject = dict[str, Any]


def _origin(url: str) -> tuple[str, str]:
    parts = urlsplit(url)
    return parts.scheme.lower(), parts.netloc.lower()


def _response_request_id(response: httpx.Response) -> str | None:
    for name in _RESPONSE_REQUEST_ID_HEADERS:
        value: str | None = response.headers.get(name)
        if value:
            return value
    return None


def _correlation_id(response: httpx.Response) -> str | None:
    value: str | None = response.request.headers.get(CLIENT_REQUEST_ID_HEADER)
    return value


# Power BI wraps identifiers in its messages in privacy markers. Observed live: ``<oii>customers_clean</oii>`` in
# refresh errors and ``<eupi>{principal object id}</eupi>`` in a 403 body. No Microsoft page lists the markers the
# service can emit; the set below is the vocabulary of Microsoft's data classification (end-user identifiable
# information EUII, end-user pseudonymous identifiers EUPI, organization identifiable information OII, customer
# content CCON) plus the short forms pii/pi/ii, so unknown members of the family are handled the same way.
#
# * End-user markers and customer content identify a person or carry the customer's own data: the content is
#   REDACTED, never passed on to evidence, logs, audit or a model.
# * OII markers wrap object names (a table, a tenant): the content is what makes the error diagnosable, so only
#   the markers are removed.
_REDACTED_MARKERS = "eupi|euii|pii|pi|ccon"
_UNWRAPPED_MARKERS = "oii|ii"
_PAIRED_PERSONAL = re.compile(rf"<(?P<tag>{_REDACTED_MARKERS})>.*?<\\?/(?P=tag)>", re.IGNORECASE | re.DOTALL)
_UNCLOSED_PERSONAL = re.compile(rf"<(?:{_REDACTED_MARKERS})>.*\Z", re.IGNORECASE | re.DOTALL)
_ANY_MARKER = re.compile(rf"<\\?/?(?:{_REDACTED_MARKERS}|{_UNWRAPPED_MARKERS})>", re.IGNORECASE)
REDACTED = "[redacted]"


def strip_privacy_tags(text: str) -> str:
    """Remove Power BI privacy markers: redact end-user and customer-content ones, unwrap the rest.

    ``user <eupi>abc</eupi>`` becomes ``user [redacted]``; ``table '<oii>t</oii>'`` becomes ``table 't'``. An opening
    end-user marker that is never closed redacts everything after it. Escaped closers (``<\\/oii>``, as found inside
    JSON strings) are handled.
    """
    text = _PAIRED_PERSONAL.sub(REDACTED, text)
    text = _UNCLOSED_PERSONAL.sub(REDACTED, text)
    return _ANY_MARKER.sub("", text)


def scrub_markers(value: Any) -> Any:
    """:func:`strip_privacy_tags` over every string inside a JSON-like value."""
    if isinstance(value, str):
        return strip_privacy_tags(value)
    if isinstance(value, list):
        return [scrub_markers(v) for v in value]
    if isinstance(value, dict):
        return {k: scrub_markers(v) for k, v in value.items()}
    return value


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = strip_privacy_tags(str(value)).strip()
    return redact_text(text[:_MAX_ERROR_TEXT]) if text else None


def parse_retry_after(response: httpx.Response, *, cap: float) -> float | None:
    """``Retry-After`` in seconds (delta-seconds or HTTP-date), clamped to ``[0, cap]``."""
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        try:
            when = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        seconds = (when - datetime.now(UTC)).total_seconds()
    if not math.isfinite(seconds):
        return None
    return min(max(seconds, 0.0), cap)


def parse_error_body(response: httpx.Response) -> tuple[str | None, str | None, str | None, dict[str, Any]]:
    """``(error_code, message, request_id, details)`` from a Fabric or Power BI error body.

    Fabric: ``{"errorCode", "message", "requestId", "moreDetails", "relatedResource"}``.
    Power BI: ``{"error": {"code", "message", "details", "pbi.error"}}``.
    """
    try:
        body = response.json()
    except ValueError:
        text = response.text.strip()
        return None, None, None, ({"body": redact_text(strip_privacy_tags(text)[:500])} if text else {})
    if not isinstance(body, dict):
        return None, None, None, {}
    details: dict[str, Any] = {}
    error = body.get("error")
    if isinstance(error, dict):
        if error.get("details"):
            details["more_details"] = redact(scrub_markers(error["details"]))
        if isinstance(pbi := error.get("pbi.error"), dict):
            details["pbi_error"] = redact(scrub_markers(pbi))
        return _text(error.get("code")), _text(error.get("message")), _text(body.get("requestId")), details
    if body.get("moreDetails"):
        details["more_details"] = redact(scrub_markers(body["moreDetails"]))
    if body.get("relatedResource"):
        details["related_resource"] = redact(scrub_markers(body["relatedResource"]))
    return _text(body.get("errorCode")), _text(body.get("message")), _text(body.get("requestId")), details


class FabricHttp:
    """Authenticated JSON client with retries, correlation ids, error mapping and pagination.

    Retries (exponential backoff + jitter, or the server's ``Retry-After``) apply to 429, 500, 502,
    503, 504 and transport errors. FabricOps only issues reads through this class, including the
    read-only POST query endpoints, so retrying is always safe.
    """

    def __init__(
        self,
        token_provider: TokenProvider,
        *,
        timeout: float = 30.0,
        max_retries: int = 4,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[], float] = random.random,
        backoff_base_seconds: float = 1.0,
        backoff_max_seconds: float = 30.0,
        max_retry_after_seconds: float = 60.0,
        max_pages: int = 100,
    ) -> None:
        if max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        if max_pages < 1:
            raise ValueError("max_pages must be >= 1")
        self._tokens = token_provider
        self._max_retries = max_retries
        self._sleep = sleep
        self._jitter = jitter
        self._backoff_base = backoff_base_seconds
        self._backoff_max = backoff_max_seconds
        self._max_retry_after = max_retry_after_seconds
        self._max_pages = max_pages
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout),
            transport=transport,
            follow_redirects=False,
            headers={"Accept": "application/json", "User-Agent": f"orqentis-fabricops/{__version__}"},
        )

    def _backoff(self, attempt: int) -> float:
        delay = self._backoff_base * 2 ** (attempt - 1) + self._jitter() * self._backoff_base
        return float(min(self._backoff_max, delay))

    async def request(
        self,
        method: str,
        url: str,
        *,
        scope: str,
        params: QueryParams | None = None,
        json: Any = None,
    ) -> httpx.Response:
        """Send one logical request (with retries). Returns a 2xx response or raises a FabricError."""
        method = method.upper()
        correlation_id = str(uuid4())
        parts = urlsplit(url)
        host, path = parts.netloc, parts.path
        attempt = 0
        while True:
            attempt += 1
            token = await self._tokens.get_token(scope)
            headers = {"Authorization": f"Bearer {token}", CLIENT_REQUEST_ID_HEADER: correlation_id}
            started = time.perf_counter()
            try:
                response = await self._client.request(method, url, params=params, json=json, headers=headers)
            except httpx.TransportError as exc:
                will_retry = attempt <= self._max_retries
                self._log(
                    logging.WARNING,
                    method=method,
                    host=host,
                    path=path,
                    status=None,
                    attempt=attempt,
                    started=started,
                    request_id=None,
                    correlation_id=correlation_id,
                    outcome="retry" if will_retry else "error",
                    error=type(exc).__name__,
                )
                if not will_retry:
                    raise FabricUnavailableError(
                        f"{method} {path} failed after {attempt} attempt(s): {type(exc).__name__}",
                        error_code="TransportError",
                        correlation_id=correlation_id,
                        retryable=True,
                        details={"method": method, "path": path, "attempts": attempt},
                    ) from exc
                await self._sleep(self._backoff(attempt))
                continue

            request_id = _response_request_id(response)
            status = response.status_code
            if response.is_success:
                self._log(
                    logging.INFO,
                    method=method,
                    host=host,
                    path=path,
                    status=status,
                    attempt=attempt,
                    started=started,
                    request_id=request_id,
                    correlation_id=correlation_id,
                    outcome="ok",
                )
                return response
            will_retry = status in RETRYABLE_STATUS and attempt <= self._max_retries
            self._log(
                logging.WARNING,
                method=method,
                host=host,
                path=path,
                status=status,
                attempt=attempt,
                started=started,
                request_id=request_id,
                correlation_id=correlation_id,
                outcome="retry" if will_retry else "error",
            )
            if not will_retry:
                raise self._error(response, method=method, path=path, correlation_id=correlation_id, attempts=attempt)
            retry_after = parse_retry_after(response, cap=self._max_retry_after)
            await self._sleep(retry_after if retry_after is not None else self._backoff(attempt))

    async def get_json(self, url: str, *, scope: str, params: QueryParams | None = None) -> JsonObject:
        return self._json_object(await self.request("GET", url, scope=scope, params=params))

    async def post_json(
        self, url: str, *, scope: str, json: Any = None, params: QueryParams | None = None
    ) -> JsonObject:
        return self._json_object(await self.request("POST", url, scope=scope, params=params, json=json))

    async def paginate(
        self, url: str, *, scope: str, params: QueryParams | None = None, items_key: str = "value"
    ) -> list[JsonObject]:
        """Collect every page of a Fabric list API.

        Follows ``continuationUri`` (only when it stays on the original host, so the bearer token is
        never sent elsewhere) or else ``continuationToken``. Stops with a warning on a repeated token or
        after ``max_pages`` pages rather than looping forever.
        """
        origin, log_extra = _origin(url), {"http_path": urlsplit(url).path}
        items: list[JsonObject] = []
        next_url: str = url
        next_params: QueryParams | None = params
        seen: set[str] = set()
        for _ in range(self._max_pages):
            body = await self.get_json(next_url, scope=scope, params=next_params)
            page = body.get(items_key)
            if isinstance(page, list):
                items.extend(entry for entry in page if isinstance(entry, dict))
            uri, token = body.get("continuationUri"), body.get("continuationToken")
            if not uri and not token:
                return items
            marker = str(token or uri)
            if marker in seen:
                _log.warning("fabric pagination stopped: repeated continuation", extra=log_extra)
                return items
            seen.add(marker)
            if isinstance(uri, str) and uri and _origin(uri) == origin:
                next_url, next_params = uri, None
            elif isinstance(token, str) and token:
                next_url, next_params = url, {**(params or {}), "continuationToken": token}
            else:
                _log.warning("fabric pagination stopped: continuation is off-host", extra=log_extra)
                return items
        _log.warning("fabric pagination stopped: page limit reached", extra={**log_extra, "max_pages": self._max_pages})
        return items

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------------------------- helpers
    def _json_object(self, response: httpx.Response) -> JsonObject:
        if not response.content:
            return {}
        try:
            body = response.json()
        except ValueError:
            raise FabricError(
                f"{response.request.method} {response.request.url.path} returned a non-JSON body",
                status_code=response.status_code,
                error_code="InvalidResponse",
                request_id=_response_request_id(response),
                correlation_id=_correlation_id(response),
            ) from None
        if isinstance(body, dict):
            return body
        if isinstance(body, list):
            return {"value": body}
        raise FabricError(
            f"{response.request.method} {response.request.url.path} returned an unexpected JSON payload",
            status_code=response.status_code,
            error_code="InvalidResponse",
            request_id=_response_request_id(response),
            correlation_id=_correlation_id(response),
        )

    def _error(
        self, response: httpx.Response, *, method: str, path: str, correlation_id: str, attempts: int
    ) -> FabricError:
        status = response.status_code
        error_code, api_message, body_request_id, details = parse_error_body(response)
        request_id = _response_request_id(response) or body_request_id
        details = {**details, "method": method, "path": path, "attempts": attempts}
        message = f"{method} {path} failed with HTTP {status}" + (f": {api_message}" if api_message else "")
        if status == 429:
            return FabricRateLimitedError(
                message,
                retry_after_seconds=parse_retry_after(response, cap=self._max_retry_after),
                status_code=status,
                error_code=error_code or "TooManyRequests",
                request_id=request_id,
                correlation_id=correlation_id,
                details=details,
            )
        error_cls: type[FabricError]
        if status == 404:
            error_cls = FabricNotFoundError
        elif status in (401, 403):
            error_cls = FabricAuthError
        elif status >= 500:
            error_cls = FabricUnavailableError
        else:
            error_cls = FabricError
        return error_cls(
            message,
            status_code=status,
            error_code=error_code,
            request_id=request_id,
            correlation_id=correlation_id,
            retryable=status in RETRYABLE_STATUS,
            details=details,
        )

    @staticmethod
    def _log(
        level: int,
        *,
        method: str,
        host: str,
        path: str,
        status: int | None,
        attempt: int,
        started: float,
        request_id: str | None,
        correlation_id: str,
        outcome: str,
        error: str | None = None,
    ) -> None:
        extra: dict[str, Any] = {
            "http_method": method,
            "http_host": host,
            "http_path": path,
            "status_code": status,
            "attempt": attempt,
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            "request_id": request_id,
            "correlation_id": correlation_id,
            "outcome": outcome,
        }
        if error is not None:
            extra["error"] = error
        _log.log(level, "fabric http request", extra=extra)
