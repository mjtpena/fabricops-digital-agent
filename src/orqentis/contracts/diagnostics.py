"""Turn JSON syntax errors and Pydantic validation errors into line-oriented :class:`ContractViolation`."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from orqentis.contracts.errors import ContractsFileNotFoundError, ContractsInvalidError, ContractViolation, Severity
from orqentis.contracts.locator import Locations, locate, pointer_for

MAX_FILE_BYTES = 8 * 1024 * 1024  # a contracts file is reviewed by humans; anything bigger is a mistake


def read_text(path: Path) -> str:
    """UTF-8 text of ``path`` (see :func:`read_document`)."""
    return read_document(path)[0]


def read_document(path: Path) -> tuple[str, str]:
    """``(text, sha256 of the file's bytes)`` for ``path``, read once.

    Raises :class:`ContractsFileNotFoundError` when nothing is there, and :class:`ContractsInvalidError` when
    something is there but cannot be a contracts file (a directory, too large, not text, unreadable).
    """
    try:
        if not path.exists():
            raise ContractsFileNotFoundError(path)
        reason = None
        if not path.is_file():
            reason = "the path is not a file"
        elif path.stat().st_size > MAX_FILE_BYTES:
            reason = f"the file is larger than {MAX_FILE_BYTES // (1024 * 1024)} MiB"
        if reason is None:
            data = path.read_bytes()
            # utf-8-sig tolerates the BOM some Windows editors add
            return data.decode("utf-8-sig"), hashlib.sha256(data).hexdigest()
    except UnicodeDecodeError:
        reason = "the file is not valid UTF-8 text"
    except OSError as exc:
        reason = f"the file cannot be read ({type(exc).__name__})"
    raise ContractsInvalidError(path, (ContractViolation("", reason),))


def violation(locations: Locations, pointer: str, message: str, severity: Severity = "error") -> ContractViolation:
    position = locations.find(pointer)
    return ContractViolation(
        pointer=pointer,
        message=message,
        line=position[0] if position else None,
        column=position[1] if position else None,
        severity=severity,
    )


def parse_json(text: str) -> tuple[Any, Locations, list[ContractViolation]]:
    """Parse ``text``; returns ``(value, locations, violations)`` with ``value`` None on a syntax error."""
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, Locations(), [ContractViolation("", f"invalid JSON: {exc.msg}", exc.lineno, exc.colno)]
    except RecursionError:
        return None, Locations(), [ContractViolation("", "invalid JSON: the document is nested too deeply")]
    locations = locate(text)
    # A repeated key is reported at the repeated key itself, not at the enclosing object.
    violations = [
        ContractViolation(parent, f"duplicate key '{key}' (JSON readers keep only the last value)", line, col)
        for parent, key, line, col in locations.duplicate_keys
    ]
    return value, locations, violations


def pydantic_violations(exc: ValidationError, locations: Locations) -> list[ContractViolation]:
    """One violation per Pydantic error. The offending input value is never included."""
    out: list[ContractViolation] = []
    for error in exc.errors(include_url=False, include_context=False, include_input=False):
        message = error["msg"].removeprefix("Value error, ")
        if error["type"] == "extra_forbidden":
            message = "unknown field (check the spelling against the schema)"
        out.append(violation(locations, pointer_for(error["loc"]), message))
    return out
