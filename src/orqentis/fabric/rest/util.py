"""Small helpers shared by the REST adapters (kept out of ``client.py`` so adapters can use them without a cycle)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from orqentis.fabric.errors import FabricError


def parse_datetime(value: Any) -> datetime | None:
    """Fabric/Power BI timestamps (``Z`` suffix, 7 fractional digits, or naive UTC) as aware UTC datetimes."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.year <= 1:  # .NET DateTime.MinValue placeholder for "not set"
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def segment(value: str) -> str:
    """One URL path segment, percent-encoded; refuses empty and dot segments so an id can never walk the path."""
    if not value or value in (".", ".."):
        raise FabricError(f"Invalid Fabric identifier {value!r}", error_code="InvalidIdentifier")
    return quote(value, safe="")
