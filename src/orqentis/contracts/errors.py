"""Typed errors for the contracts file: a clear message naming the path and every violation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

Severity = Literal["error", "warning"]

# `str(error)` lists at most this many violations (logs stay bounded); `error.violations` keeps all of them.
_MAX_LISTED = 25


@dataclass(frozen=True)
class ContractViolation:
    """One problem in a contracts file. Messages never echo values from the file, only ids and positions."""

    pointer: str  # JSON pointer, e.g. /dependencies/3/target_item_id ("" is the whole document)
    message: str
    line: int | None = None
    column: int | None = None
    severity: Severity = "error"

    def format(self, path: Path | str | None = None) -> str:
        """``path:line:column: severity: pointer: message`` (compiler style, one line per violation)."""
        where = str(path) if path is not None else "<contracts>"
        if self.line is not None:
            where += f":{self.line}:{self.column or 1}"
        return f"{where}: {self.severity}: {self.pointer or '/'}: {self.message}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "pointer": self.pointer,
            "message": self.message,
            "line": self.line,
            "column": self.column,
            "severity": self.severity,
        }


class ContractsError(Exception):
    """The contracts file is missing, unreadable or invalid.

    ``str(error)`` names the path and lists the violations, one per line. Nothing from the file's content
    is included beyond ids, so the message is safe to log and to show in a readiness response.
    """

    summary = "Contracts are not usable"

    def __init__(
        self, path: Path | str | None, violations: tuple[ContractViolation, ...], *, summary: str | None = None
    ) -> None:
        self.path = path
        self.violations = violations
        self.summary = summary or type(self).summary
        super().__init__(self._render())

    def _render(self) -> str:
        shown = self.violations[:_MAX_LISTED]
        lines = [f"{self.summary}: {self.path}" if self.path is not None else self.summary]
        lines += [f"  {v.format(self.path)}" for v in shown]
        if len(self.violations) > len(shown):
            lines.append(f"  ... and {len(self.violations) - len(shown)} more")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": type(self).__name__,
            "message": self.summary,
            "path": str(self.path) if self.path is not None else None,
            "violations": [v.to_dict() for v in self.violations],
        }


class ContractsNotConfiguredError(ContractsError):
    """Rest mode without ``ORQENTIS_CONTRACTS_PATH``."""

    summary = (
        "A contracts source is required in rest mode (environments, topology, data products, SLAs): "
        "ORQENTIS_CONTRACTS_PATH, or the contracts blob of the postgres storage backend"
    )

    def __init__(self) -> None:
        super().__init__(
            None,
            (ContractViolation("", "set ORQENTIS_CONTRACTS_PATH to the contracts file, or use the postgres backend"),),
        )


class ContractsFileNotFoundError(ContractsError):
    summary = "Contracts file not found"

    def __init__(self, path: Path | str, reason: str = "the file does not exist") -> None:
        super().__init__(path, (ContractViolation("", reason),))


class ContractsInvalidError(ContractsError):
    """The file exists but is not valid JSON, does not match the schema, or is internally inconsistent."""

    summary = "Contracts file is invalid"

    def __init__(self, path: Path | str, violations: tuple[ContractViolation, ...]) -> None:
        super().__init__(path, violations)
