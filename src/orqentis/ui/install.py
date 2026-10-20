"""Wires the UI into the FastAPI app: state, routes, response headers, HTML errors and the cross-origin guard."""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request
from starlette.responses import JSONResponse, Response

from orqentis.config.settings import Settings
from orqentis.ui import routes
from orqentis.ui.paths import is_ui_path
from orqentis.ui.render import UiState, build_state, render_error
from orqentis.ui.security import CrossOriginGuardMiddleware, UiSecurityHeadersMiddleware

_CARRIED_HEADERS = ("retry-after", "www-authenticate")


def _error_fields(response: JSONResponse) -> tuple[str, str]:
    """The ``(code, message)`` of an error response produced by one of the application's JSON handlers."""
    try:
        body: Any = json.loads(bytes(response.body))
    except ValueError:
        return "ERROR", "The request could not be completed"
    if isinstance(body, dict):
        message = body.get("message")
        detail = body.get("detail")
        return (
            str(body.get("error") or "ERROR"),
            message if isinstance(message, str) else detail if isinstance(detail, str) else "The request was not valid",
        )
    return "ERROR", "The request could not be completed"


def _html_for_ui(ui: UiState, handler: Callable[..., Any]) -> Callable[[Request, Exception], Any]:
    """Wrap an exception handler: under ``/ui`` its JSON error answer becomes an HTML page with the same status."""

    async def wrapped(request: Request, exc: Exception) -> Response:
        outcome = handler(request, exc)
        response: Response = await outcome if inspect.isawaitable(outcome) else outcome
        if not is_ui_path(request.url.path) or not isinstance(response, JSONResponse):
            return response
        code, message = _error_fields(response)
        page = render_error(ui, response.status_code, code, message)
        for name in _CARRIED_HEADERS:
            if name in response.headers:
                page.headers[name] = response.headers[name]
        return page

    return wrapped


def install_cross_origin_guard(app: FastAPI, settings: Settings) -> None:
    """CSRF defence for every state-changing request, API and UI alike (the UI makes cookie sessions possible)."""
    app.add_middleware(CrossOriginGuardMiddleware, public_base_url=settings.public_base_url)


def install_ui(app: FastAPI, settings: Settings) -> None:
    """Mount ``/ui``. Call after every exception handler is registered, because those are wrapped here."""
    ui = build_state(settings)
    app.state.ui = ui
    app.state.ui_error_page = lambda status, code, message: render_error(ui, status, code, message)
    for exc_class, handler in list(app.exception_handlers.items()):
        app.add_exception_handler(exc_class, _html_for_ui(ui, handler))
    app.include_router(routes.router)
    app.add_middleware(UiSecurityHeadersMiddleware)  # outermost: also covers refusals made by the guard and size limit
