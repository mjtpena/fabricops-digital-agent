"""Audit epoch markers as blobs (:class:`~orqentis.observability.epochs.EpochMarkerStore`).

One small JSON object per epoch, ``epoch-NNNN.json``, uploaded with ``If-None-Match: *`` (write-once) into the
``audit-epochs`` container. The container has version-level immutability like the other evidence containers, and its
role assignments are the point: the application's identity has Storage Blob Data *Reader* only, so it can read the
markers (verification, the anchor task) but cannot create, change or delete one; only the operator group that opens
an epoch after a restore has Contributor. The application therefore never calls :meth:`put`.
"""

from __future__ import annotations

from collections.abc import Sequence

from azure.core.exceptions import HttpResponseError, ResourceExistsError
from azure.storage.blob import ContentSettings
from azure.storage.blob.aio import ContainerClient

from orqentis.observability.epochs import (
    EpochMarker,
    EpochMarkerAccessError,
    EpochMarkerConflictError,
    parse_marker_name,
)
from orqentis.storage.blob.evidence import normalise_prefix


class BlobEpochMarkerStore:
    def __init__(self, container: ContainerClient, *, path_prefix: str = "") -> None:
        self._container = container
        self._prefix = normalise_prefix(path_prefix)

    async def put(self, marker: EpochMarker) -> bool:
        name = f"{self._prefix}{marker.name}"
        try:
            await self._container.upload_blob(
                name,
                marker.model_dump_json().encode("utf-8"),
                overwrite=False,
                content_settings=ContentSettings(content_type="application/json"),
            )
        except ResourceExistsError:
            downloader = await self._container.download_blob(name)
            existing = EpochMarker.model_validate_json(await downloader.readall())
            if existing.sequence == marker.sequence and existing.hash == marker.hash:
                return False
            raise EpochMarkerConflictError(f"a different marker exists for audit epoch {marker.epoch}") from None
        except HttpResponseError as exc:
            if exc.status_code in (401, 403):
                # The application's identity is Reader on this container by design; an operator is Contributor.
                raise EpochMarkerAccessError(
                    "this identity cannot write to the audit epoch container (HTTP "
                    f"{exc.status_code}); opening an audit epoch needs Storage Blob Data Contributor on it"
                ) from None
            raise
        return True

    async def all(self) -> Sequence[EpochMarker]:
        markers: list[EpochMarker] = []
        async for blob in self._container.list_blobs(name_starts_with=f"{self._prefix}epoch-"):
            if parse_marker_name(blob.name[len(self._prefix) :]) is None:
                continue
            downloader = await self._container.download_blob(blob.name)
            markers.append(EpochMarker.model_validate_json(await downloader.readall()))
        return sorted(markers, key=lambda m: m.epoch)
