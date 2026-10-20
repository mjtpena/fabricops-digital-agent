"""URL paths of the web UI. The single place that knows them (routes, templates and notification deep links)."""

from __future__ import annotations

from urllib.parse import quote

UI_PREFIX = "/ui"
ASSETS_PATH = f"{UI_PREFIX}/assets/ui.css"


def is_ui_path(path: str) -> bool:
    return path == UI_PREFIX or path.startswith(f"{UI_PREFIX}/")


def incidents_path() -> str:
    return f"{UI_PREFIX}/incidents"


def incident_path(incident_id: str) -> str:
    return f"{UI_PREFIX}/incidents/{quote(incident_id, safe='')}"


def report_path(incident_id: str) -> str:
    return f"{incident_path(incident_id)}/report"


def approvals_path() -> str:
    return f"{UI_PREFIX}/approvals"


def approval_path(approval_id: str) -> str:
    return f"{UI_PREFIX}/approvals/{quote(approval_id, safe='')}"


def audit_path() -> str:
    return f"{UI_PREFIX}/audit"
