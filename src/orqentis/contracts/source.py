"""Where the contracts file comes from, and how a running app picks up a new version of it.

* Local and mock runs read a file (``ORQENTIS_CONTRACTS_PATH``): :class:`FileContractsSource`.
* Deployed environments read a **blob** next to the evidence (``orqentis.storage.blob.BlobContractsSource``), because
  the container has no shared volume any more. A person uploads the reviewed file; the app is never given write access.

:func:`load_contracts_from` is the one place that turns a source's bytes into validated :class:`EstateContracts`: the
same typed :mod:`~orqentis.contracts.errors` as before, naming where the document came from and every violation, and
never echoing values from it. :class:`ContractsHolder` keeps the contracts in force and re-reads the source on demand
(the catalog refresh does, on every replica): an unchanged SHA-256 is a no-op, a changed one is validated and applied,
and an invalid or missing one raises the typed error while the **last good contracts stay in force**.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from orqentis.contracts.diagnostics import MAX_FILE_BYTES, read_document
from orqentis.contracts.errors import ContractsInvalidError, ContractViolation
from orqentis.contracts.model import EstateContracts
from orqentis.contracts.validate import validate_text
from orqentis.observability.logging import get_logger

_log = get_logger(__name__)


@dataclass(frozen=True)
class ContractsDocument:
    """One read of the contracts file: its text, the SHA-256 of its bytes, and where and which version it was."""

    text: str
    sha256: str
    location: str  # a path, or ``blob:<container>/<name>``
    version: str | None = None  # the blob's version id (or ETag); None for a file


class ContractsSource(Protocol):
    @property
    def location(self) -> str: ...

    async def read(self) -> ContractsDocument:
        """Read the current document. Raises :class:`ContractsFileNotFoundError` / :class:`ContractsInvalidError`."""
        ...


def decode_document(data: bytes, location: str, *, version: str | None = None) -> ContractsDocument:
    """A document from raw bytes (UTF-8, optionally with a BOM), refusing anything that cannot be a contracts file."""
    if len(data) > MAX_FILE_BYTES:
        raise ContractsInvalidError(
            location, (ContractViolation("", f"the file is larger than {MAX_FILE_BYTES // (1024 * 1024)} MiB"),)
        )
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ContractsInvalidError(location, (ContractViolation("", "the file is not valid UTF-8 text"),)) from None
    return ContractsDocument(text, hashlib.sha256(data).hexdigest(), location, version)


class FileContractsSource:
    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def location(self) -> str:
        return str(self._path)

    async def read(self) -> ContractsDocument:
        text, digest = await asyncio.to_thread(read_document, self._path)
        return ContractsDocument(text, digest, str(self._path))


def load_contracts_document(document: ContractsDocument) -> EstateContracts:
    """Validate a document. Raises :class:`ContractsInvalidError` naming its location and every violation."""
    report = validate_text(document.text, document.location)
    if report.errors or report.contracts is None:
        raise ContractsInvalidError(document.location, report.errors)
    if report.warnings:
        _log.warning(
            "contracts loaded with warnings; run `orqentis contracts validate` to review them",
            extra={"contracts_path": document.location, "warning_count": len(report.warnings)},
        )
    return report.contracts.with_source_sha256(document.sha256)


async def load_contracts_from(source: ContractsSource) -> tuple[EstateContracts, ContractsDocument]:
    document = await source.read()
    return load_contracts_document(document), document


class ContractsHolder:
    """The contracts in force, and a way to re-read them. ``apply`` pushes new contracts into the live adapters."""

    def __init__(
        self,
        source: ContractsSource,
        contracts: EstateContracts,
        document: ContractsDocument,
        apply: Callable[[EstateContracts], None],
    ) -> None:
        self._source = source
        self._contracts = contracts
        self._document = document
        self._apply = apply
        self._lock = asyncio.Lock()

    @property
    def contracts(self) -> EstateContracts:
        return self._contracts

    @property
    def document(self) -> ContractsDocument:
        return self._document

    async def reload(self) -> bool:
        """Re-read the source; apply and return True when the content changed, False when it did not.

        Raises the typed contracts error when the source is missing or invalid (nothing is applied: the contracts that
        were in force stay in force). Callers that run on a schedule report that once and retry on the next run.
        """
        async with self._lock:  # a scheduled refresh and a replica's own refresh never apply the same change twice
            document = await self._source.read()
            if document.sha256 == self._document.sha256:
                return False
            contracts = load_contracts_document(document)
            self._apply(contracts)
            previous, self._contracts, self._document = self._document, contracts, document
            _log.info(
                "contracts reloaded",
                extra={
                    "contracts_path": document.location,
                    "contracts_sha256": document.sha256,
                    "previous_sha256": previous.sha256,
                    "contracts_version": document.version,
                },
            )
            return True
