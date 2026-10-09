"""Structured JSON logging with trace/incident correlation and redaction."""

from __future__ import annotations

import contextlib
import json
import logging
import sys
from collections.abc import Iterator
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from orqentis.observability.redaction import redact

_trace_id: ContextVar[str | None] = ContextVar("orqentis_trace_id", default=None)
_incident_id: ContextVar[str | None] = ContextVar("orqentis_incident_id", default=None)

_STANDARD_ATTRS = frozenset(vars(logging.LogRecord("", 0, "", 0, "", (), None)).keys()) | {"message", "asctime"}


@contextlib.contextmanager
def log_context(*, trace_id: str | None = None, incident_id: str | None = None) -> Iterator[None]:
    tokens = []
    if trace_id is not None:
        tokens.append((_trace_id, _trace_id.set(trace_id)))
    if incident_id is not None:
        tokens.append((_incident_id, _incident_id.set(incident_id)))
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


def current_trace_id() -> str | None:
    return _trace_id.get()


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if (trace_id := _trace_id.get()) is not None:
            payload["trace_id"] = trace_id
        if (incident_id := _incident_id.get()) is not None:
            payload["incident_id"] = incident_id
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(redact(payload), default=str)


class ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        extras = {k: v for k, v in record.__dict__.items() if k not in _STANDARD_ATTRS and not k.startswith("_")}
        base = f"{record.levelname:<7} {record.name}: {record.getMessage()}"
        if extras:
            base += " " + json.dumps(redact(extras), default=str)
        return base


def configure_logging(
    level: str = "INFO", fmt: str = "json", log_file: Path | None = None, *, stream: bool = True
) -> None:
    root = logging.getLogger("orqentis")
    root.handlers.clear()
    root.setLevel(level.upper())
    root.propagate = False
    formatter: logging.Formatter = JsonFormatter() if fmt == "json" else ConsoleFormatter()
    if stream:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(formatter)
        root.addHandler(handler)
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(JsonFormatter())
        root.addHandler(file_handler)
    if not root.handlers:
        root.addHandler(logging.NullHandler())


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name if name.startswith("orqentis") else f"orqentis.{name}")
