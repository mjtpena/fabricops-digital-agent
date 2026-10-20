"""Audit anchors as immutable blobs (:class:`~orqentis.observability.anchors.AuditAnchorStore`).

Each anchor is a small JSON object named ``epoch-NNNN/<sequence, 12 digits>-<hash, 64 hex>.json`` and uploaded with
``If-None-Match: *`` (write-once). In the deployed storage account the container has version-level immutability, so the
application identity can create anchors but nobody, the application included, can change or delete one before its
retention ends. Listing is lexicographic, which for zero-padded sequences is numeric order, and the name alone says
everything verification needs (epoch, sequence, hash).
"""

from __future__ import annotations

from azure.core.exceptions import ResourceExistsError
from azure.storage.blob import ContentSettings
from azure.storage.blob.aio import ContainerClient

from orqentis.observability.anchors import AnchorRef, AuditAnchor, parse_anchor_name, pick_latest
from orqentis.storage.blob.evidence import normalise_prefix


class BlobAuditAnchorStore:
    def __init__(self, container: ContainerClient, *, path_prefix: str = "") -> None:
        self._container = container
        self._prefix = normalise_prefix(path_prefix)

    async def put(self, anchor: AuditAnchor) -> bool:
        try:
            await self._container.upload_blob(
                f"{self._prefix}{anchor.name}",
                anchor.model_dump_json().encode("utf-8"),
                overwrite=False,
                content_settings=ContentSettings(content_type="application/json"),
            )
        except ResourceExistsError:
            return False
        return True

    async def refs(self, epoch: int) -> list[AnchorRef]:
        folder = f"{self._prefix}epoch-{epoch:04d}/"
        found: list[AnchorRef] = []
        async for blob in self._container.list_blobs(name_starts_with=folder):
            parsed = parse_anchor_name(blob.name[len(self._prefix) :])
            if parsed is not None:
                found.append(AnchorRef(*parsed))
        return sorted(found, key=lambda r: (r.sequence, r.hash))

    async def latest(self, epoch: int) -> AuditAnchor | None:
        refs = await self.refs(epoch)
        if not refs:
            return None
        top = refs[-1].sequence
        # Every object at the highest sequence (normally one; two with different hashes is a conflict to report).
        anchors = []
        for ref in (r for r in refs if r.sequence == top):
            name = f"{self._prefix}epoch-{ref.epoch:04d}/{ref.sequence:012d}-{ref.hash}.json"
            downloader = await self._container.download_blob(name)
            anchors.append(AuditAnchor.model_validate_json(await downloader.readall()))
        return pick_latest(anchors)
