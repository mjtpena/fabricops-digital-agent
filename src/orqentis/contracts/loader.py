"""Loading the contracts file the application runs with."""

from __future__ import annotations

from pathlib import Path

from orqentis.contracts.errors import ContractsInvalidError
from orqentis.contracts.model import EstateContracts
from orqentis.contracts.validate import validate_file
from orqentis.observability.logging import get_logger

_log = get_logger(__name__)


def load_contracts(path: Path) -> EstateContracts:
    """Load and validate declared contracts. Accepts a full estate file or a contracts-only file.

    Raises a :class:`~orqentis.contracts.errors.ContractsError` whose message names the path and every
    violation (JSON syntax, schema, duplicate ids and edges, dangling references inside the file, invalid
    time zones). It never logs or echoes the file's content. Warnings (unreviewed drafts, unchecked
    references) do not stop startup; only their count is logged, because ``orqentis contracts validate``
    is where they are read.
    """
    report = validate_file(path)
    if report.errors or report.contracts is None:
        raise ContractsInvalidError(path, report.errors)
    if report.warnings:
        _log.warning(
            "contracts loaded with warnings; run `orqentis contracts validate` to review them",
            extra={"contracts_path": str(path), "warning_count": len(report.warnings)},
        )
    return report.contracts
