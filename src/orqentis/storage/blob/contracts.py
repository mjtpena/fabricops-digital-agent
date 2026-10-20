"""The estate contracts file as a blob (:class:`~orqentis.contracts.source.ContractsSource`).

Deployed environments have no shared volume, so the reviewed contracts file lives in a container of the evidence
storage account (``contracts/estate.json`` by default). A person uploads it (``az storage blob upload --auth-mode
login``); the application only ever **reads** it with its managed identity (Storage Blob Data Reader on that container
alone). The container is versioned and soft-deleted, so every upload stays readable and the SHA-256 of the version in
force is audited on every (re)load.

Failures are the typed contracts errors, with the blob's address in the message and nothing from its content: a missing
blob is :class:`ContractsFileNotFoundError`, one that cannot be read or is not a contracts document is
:class:`ContractsInvalidError`. Network errors propagate unchanged (the caller retries).
"""

from __future__ import annotations

from azure.core.exceptions import HttpResponseError, ResourceNotFoundError
from azure.storage.blob.aio import ContainerClient

from orqentis.contracts.diagnostics import MAX_FILE_BYTES
from orqentis.contracts.errors import ContractsFileNotFoundError, ContractsInvalidError, ContractViolation
from orqentis.contracts.source import ContractsDocument, decode_document
from orqentis.storage.blob.evidence import normalise_prefix


class BlobContractsSource:
    def __init__(self, container: ContainerClient, blob_name: str, *, path_prefix: str = "") -> None:
        if not blob_name or blob_name.startswith("/") or ".." in blob_name.split("/"):
            raise ValueError(f"Invalid contracts blob name: {blob_name!r}")
        self._container = container
        self._name = f"{normalise_prefix(path_prefix)}{blob_name}"
        self._location = f"blob:{container.container_name}/{self._name}"

    @property
    def location(self) -> str:
        return self._location

    async def read(self) -> ContractsDocument:
        try:
            downloader = await self._container.download_blob(self._name)
        except ResourceNotFoundError:
            raise ContractsFileNotFoundError(self._location, "the blob does not exist") from None
        except HttpResponseError as exc:
            # 401/403: the role is missing (a fresh deployment) or the account is unreachable. Say which, not why.
            code = getattr(exc, "error_code", None) or "unknown"
            raise ContractsInvalidError(
                self._location, (ContractViolation("", f"the blob cannot be read (HTTP {exc.status_code}, {code})"),)
            ) from None
        if downloader.size > MAX_FILE_BYTES:
            raise ContractsInvalidError(
                self._location,
                (ContractViolation("", f"the file is larger than {MAX_FILE_BYTES // (1024 * 1024)} MiB"),),
            )
        data = await downloader.readall()
        properties = downloader.properties
        version = getattr(properties, "version_id", None) or getattr(properties, "etag", None)
        return decode_document(data, self._location, version=str(version) if version else None)
