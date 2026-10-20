"""Jinja2 rendering for the UI. Autoescape is on for every template and nothing is marked safe.

Untrusted text (Fabric item names and descriptions, error messages, evidence payloads, comments) only ever reaches a
page through ``{{ ... }}``, which HTML-escapes it. There is no JavaScript, no inline style and no external resource, so
the Content-Security-Policy (``orqentis.ui.security``) is a second line of defence, not the first.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from importlib import resources
from typing import Any
from zoneinfo import ZoneInfo

import jinja2
from fastapi import Request
from starlette.responses import HTMLResponse

from orqentis import __version__
from orqentis.config.settings import ApiAuthMode, Settings
from orqentis.domain.principals import Principal
from orqentis.ui import paths
from orqentis.ui.csrf import CsrfTokens

SIGN_OUT_URL = "/.auth/logout"
_MAX_JSON_CHARS = 6000


@dataclass
class UiState:
    """Everything the UI needs per app instance; ``app.state.ui``."""

    env: jinja2.Environment
    csrf: CsrfTokens
    settings: Settings
    stylesheet: bytes


def build_environment(settings: Settings) -> jinja2.Environment:
    tz = ZoneInfo(settings.default_timezone)

    def fmt_dt(value: datetime | None) -> str:
        return value.astimezone(tz).strftime("%Y-%m-%d %H:%M %Z") if value else "-"

    def pretty_json(value: Any, limit: int = _MAX_JSON_CHARS) -> str:
        text = json.dumps(value, indent=2, sort_keys=True, default=str, ensure_ascii=False)
        return text if len(text) <= limit else text[:limit] + f"\n... [{len(text) - limit} more characters not shown]"

    env = jinja2.Environment(
        loader=jinja2.PackageLoader("orqentis.ui", "templates"),
        autoescape=jinja2.select_autoescape(enabled_extensions=("html",), default_for_string=True, default=True),
        undefined=jinja2.StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["dt"] = fmt_dt
    env.filters["pretty_json"] = pretty_json
    env.globals["paths"] = paths
    return env


def build_state(settings: Settings, *, csrf: CsrfTokens | None = None) -> UiState:
    secret = settings.ui_csrf_secret
    tokens = csrf or CsrfTokens(secret.get_secret_value().encode() if secret else None)
    stylesheet = resources.files("orqentis.ui").joinpath("static/ui.css").read_bytes()
    return UiState(env=build_environment(settings), csrf=tokens, settings=settings, stylesheet=stylesheet)


def _context(ui: UiState, principal: Principal | None) -> dict[str, Any]:
    return {
        "principal": principal,
        "csrf_token": ui.csrf.issue(principal.id) if principal else None,
        "assets_url": paths.ASSETS_PATH,
        "sign_out_url": SIGN_OUT_URL if ui.settings.api_auth_mode is ApiAuthMode.EASY_AUTH else None,
        "app_version": __version__,
        "deployment_environment": ui.settings.deployment_environment,
    }


def render_page(
    request: Request,
    template: str,
    *,
    principal: Principal | None,
    status_code: int = 200,
    **context: Any,
) -> HTMLResponse:
    ui: UiState = request.app.state.ui
    html = ui.env.get_template(template).render(**_context(ui, principal), **context)
    return HTMLResponse(html, status_code=status_code)


def render_error(ui: UiState, status: int, code: str, message: str) -> HTMLResponse:
    """An error page that does not depend on the request (used by middleware and the exception handlers)."""
    html = ui.env.get_template("error.html").render(
        **_context(ui, None), status=status, code=code, message=message, title="Error"
    )
    return HTMLResponse(html, status_code=status)
