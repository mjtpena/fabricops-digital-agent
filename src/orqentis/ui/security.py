"""Browser-facing protections: the cross-origin guard and the security headers of the UI.

**Cross-origin guard (ledger R6, ADR-0015 section 6).** An ID-token browser session is classified HUMAN, so any page the
user visits could make their browser send a state-changing request to this application with the session cookie
attached. For every ``POST``/``PUT``/``PATCH``/``DELETE`` the guard applies the "standard headers" defence, before the
request reaches authentication or the body is read:

* ``Sec-Fetch-Site``, when the browser sends it, must be ``same-origin`` (or ``none``: the user typed the address).
  ``same-site`` is refused too: a sibling sub-domain is not this application.
* ``Origin``, when present (a browser sends it on every cross-origin ``POST``, and on same-origin ones), must be this
  application's own origin: the ``Host`` (or ``X-Forwarded-Host``) the request arrived with, or the configured
  ``ORQENTIS_PUBLIC_BASE_URL``. A page on another site cannot set either header, so trusting the proxy's copy is safe.
  ``Origin: null`` (a sandboxed frame, a ``no-referrer`` form post) is a mismatch.
* On ``/ui`` a browser is the only legitimate client, so one of ``Origin`` and ``Referer`` must be present and match.
  On the API a request with none of these headers is a script or ``curl``, which is no CSRF vector, and is let through.
* The API stays JSON-only: a body that is not ``application/json`` gets 415, so a plain HTML form cannot post to it.

**Headers.** Every ``/ui`` response gets a strict Content-Security-Policy (no script at all, styles from this origin,
forms to this origin, no framing), ``X-Frame-Options: DENY``, ``X-Content-Type-Options: nosniff``,
``Referrer-Policy: same-origin`` and ``Cache-Control: no-store`` (pages hold incident data and form tokens).
"""

from __future__ import annotations

from typing import Final
from urllib.parse import urlsplit

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from orqentis.observability.logging import get_logger
from orqentis.ui.paths import is_ui_path

_log = get_logger(__name__)

MUTATING_METHODS: Final = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_SAME_ORIGIN_FETCH_SITES: Final = frozenset({"same-origin", "none"})
_DEFAULT_PORTS: Final = {"http": 80, "https": 443}

CONTENT_SECURITY_POLICY: Final = (
    "default-src 'none'; style-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
)
UI_HEADERS: Final = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "Cache-Control": "no-store",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
}


def normalise_origin(value: str) -> str | None:
    """``scheme://host[:port]`` in lower case without a default port, or ``None`` if ``value`` is not an origin."""
    try:
        parts = urlsplit(value.strip())
        host, port = parts.hostname, parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS or not host:
        return None
    shown_host = f"[{host}]" if ":" in host else host
    return f"{scheme}://{shown_host}" + (f":{port}" if port and port != _DEFAULT_PORTS[scheme] else "")


def _own_origins(headers: Headers, scope: Scope, public_base_url: str | None) -> set[str]:
    forwarded = headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    scheme = forwarded if forwarded in _DEFAULT_PORTS else str(scope.get("scheme", "http"))
    origins: set[str] = set()
    # The host the browser used: the Host header, or the one the proxy in front (Easy Auth, ingress) says it received.
    for header in ("host", "x-forwarded-host"):
        host = headers.get(header, "").split(",")[0].strip()
        if host and (own := normalise_origin(f"{scheme}://{host}")):
            origins.add(own)
    if public_base_url and (configured := normalise_origin(public_base_url)):
        origins.add(configured)
    return origins


def _is_json(content_type: str) -> bool:
    media = content_type.split(";", 1)[0].strip().lower()
    return media == "application/json" or (media.startswith("application/") and media.endswith("+json"))


def refusal_response(scope: Scope, status: int, code: str, message: str) -> Response:
    """JSON for the API, a small HTML page for the UI (rendered by the UI when it is installed)."""
    if is_ui_path(scope["path"]):
        state = getattr(scope.get("app"), "state", None)
        renderer = getattr(state, "ui_error_page", None)
        if renderer is not None:
            page: Response = renderer(status, code, message)
            return page
    return JSONResponse(status_code=status, content={"error": code, "message": message})


class CrossOriginGuardMiddleware:
    """Pure ASGI middleware; see the module docstring."""

    def __init__(self, app: ASGIApp, *, public_base_url: str | None = None) -> None:
        self._app = app
        self._public_base_url = public_base_url

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] not in MUTATING_METHODS:
            await self._app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        refusal = self._check(scope, headers)
        if refusal is not None:
            code, message, status = refusal
            _log.warning(
                "cross-origin or malformed state-changing request refused",
                extra={"code": code, "method": scope["method"], "path": scope["path"]},
            )
            await refusal_response(scope, status, code, message)(scope, receive, send)
            return
        await self._app(scope, receive, send)

    def _check(self, scope: Scope, headers: Headers) -> tuple[str, str, int] | None:
        ui = is_ui_path(scope["path"])
        site = headers.get("sec-fetch-site")
        if site is not None and site.lower() not in _SAME_ORIGIN_FETCH_SITES:
            return "CROSS_SITE_REQUEST", "State-changing requests must come from this application's own pages", 403
        origin = headers.get("origin")
        referer = headers.get("referer")
        if origin is not None or ui:
            presented = origin if origin is not None else referer
            if presented is None:
                return "ORIGIN_REQUIRED", "A state-changing UI request must carry an Origin or Referer header", 403
            presented_origin = normalise_origin(presented)
            if presented_origin is None or presented_origin not in _own_origins(headers, scope, self._public_base_url):
                return "CROSS_ORIGIN_REQUEST", "The request's origin is not this application", 403
        if not ui and self._has_body(headers) and not _is_json(headers.get("content-type", "")):
            return "UNSUPPORTED_MEDIA_TYPE", "The API accepts application/json bodies only", 415
        return None

    @staticmethod
    def _has_body(headers: Headers) -> bool:
        length = headers.get("content-length")
        return headers.get("transfer-encoding") is not None or (length is not None and length.strip() not in {"", "0"})


class UiSecurityHeadersMiddleware:
    """Adds the UI's response headers to every response under ``/ui`` (also to errors produced by other middleware)."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not is_ui_path(scope["path"]):
            await self._app(scope, receive, send)
            return
        https = (
            Headers(scope=scope).get("x-forwarded-proto", "").split(",")[0].strip().lower() == "https"
            or scope.get("scheme") == "https"
        )

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in UI_HEADERS.items():
                    if name == "Cache-Control":
                        headers.setdefault(name, value)  # the stylesheet sets its own, pages get no-store
                    else:
                        headers[name] = value
                if https:
                    headers["Strict-Transport-Security"] = "max-age=31536000"
            await send(message)

        await self._app(scope, receive, send_with_headers)
