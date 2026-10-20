"""Audit anchors as immutable blobs (:class:`~orqentis.observability.anchors.AuditAnchorStore`).

Each anchor is a small JSON object named ``epoch-NNNN/<sequence, 12 digits>-<hash prefix>.json`` and uploaded with
``If-None-Match: *`` (write-once). In the deployed storage account the container has version-level immutability, so the
application identity can create anchors but nobody, the application included, can change or delete one before its
retention ends. Listing is lexicographic, which for zero-padded sequences is numeric order.
"""

from __future__ import annotations

import re

from azure.core.exceptions import ResourceExistsError
from azure.storage.blob import ContentSettings
from azure.storage.blob.aio import ContainerClient

from orqentis.observability.anchors import AuditAnchor, parse_anchor_name, pick_latest
from orqentis.storage.blob.evidence import normalise_prefix

_EPOCH_FOLDER = re.compile(r"^epoch-(\d{4})/$")


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

    async def highest_epoch(self) -> int | None:
        # One level of "folders" (epoch-NNNN/): listing by delimiter does not read every anchor ever written.
        epochs: list[int] = []
        async for item in self._container.walk_blobs(name_starts_with=f"{self._prefix}epoch-", delimiter="/"):
            name = str(item.name)[len(self._prefix) :]
            match = _EPOCH_FOLDER.match(name)
            if match:
                epochs.append(int(match.group(1)))
        return max(epochs) if epochs else None

    async def latest(self, epoch: int) -> AuditAnchor | None:
        folder = f"{self._prefix}epoch-{epoch:04d}/"
        names = [
            blob.name
            async for blob in self._container.list_blobs(name_starts_with=folder)
            if parse_anchor_name(blob.name[len(self._prefix) :]) is not None
        ]
        if not names:
            return None
        names.sort()
        top = parse_anchor_name(names[-1][len(self._prefix) :])
        assert top is not None
        # Every object at the highest sequence (normally one; two with different hashes is a conflict to report).
        candidates = [n for n in names if (parse_anchor_name(n[len(self._prefix) :]) or (0, -1, ""))[1] == top[1]]
        anchors = []
        for name in candidates:
            downloader = await self._container.download_blob(name)
            anchors.append(AuditAnchor.model_validate_json(await downloader.readall()))
        return pick_latest(anchors)
