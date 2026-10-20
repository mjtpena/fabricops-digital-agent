"""Evidence and reports in Blob storage (:class:`~orqentis.storage.evidence_store.EvidenceStore`).

One adapter speaking the Blob API of an Azure storage account **or of OneLake** (OneLake exposes the same API: the
container is the workspace, and ``path_prefix`` the item folder, for example ``Orqentis.Lakehouse/Files/evidence``). It
authenticates with an Entra token credential only; no account key or SAS is ever used.

Write-once semantics
--------------------
* ``immutable=True`` uploads with ``If-None-Match: *``. A second write of the same name is refused by the service with
  409 and surfaces as :class:`EvidenceExistsError`, exactly like the local filesystem adapter. This holds with or
  without immutability policies configured.
* ``immutable=False`` (reports, which are re-rendered when an incident is investigated again) overwrites. The storage
  account has blob versioning and a **version-level immutability policy** (infrastructure, ``infra/bicep``), so an
  overwrite creates a new version and the previous one stays protected and readable by version id; nothing is ever
  silently destroyed.

URIs stay ``evidence://<incident>/<name>`` so incident records and reports are independent of where the bytes live.
"""

from __future__ import annotations

import json
import re
from typing import Any

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.storage.blob.aio import ContainerClient

from orqentis.observability.redaction import redact
from orqentis.storage.evidence_store import SCHEME, EvidenceExistsError

_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_CONTENT_TYPE_JSON = "application/json"
_CONTENT_TYPE_TEXT = "text/plain; charset=utf-8"


def normalise_prefix(prefix: str) -> str:
    """``''`` or a path ending in one slash; leading slashes and ``..`` are rejected."""
    cleaned = prefix.strip().strip("/")
    if not cleaned:
        return ""
    if any(part in {"", ".", ".."} for part in cleaned.split("/")):
        raise ValueError(f"Invalid blob path prefix: {prefix!r}")
    return f"{cleaned}/"


class BlobEvidenceStore:
    def __init__(self, container: ContainerClient, *, path_prefix: str = "") -> None:
        self._container = container
        self._prefix = normalise_prefix(path_prefix)

    def _blob_name(self, incident_id: str, name: str) -> str:
        for segment in (incident_id, name):
            if not _SAFE_SEGMENT.match(segment) or ".." in segment:
                raise ValueError(f"Unsafe evidence path segment: {segment!r}")
        return f"{self._prefix}{incident_id}/{name}"

    def _from_uri(self, uri: str) -> str:
        if not uri.startswith(SCHEME):
            raise ValueError(f"Not an evidence URI: {uri}")
        incident_id, _, name = uri[len(SCHEME) :].partition("/")
        return self._blob_name(incident_id, name)

    def local_path(self, uri: str) -> None:
        self._from_uri(uri)  # still validates the URI
        return None

    async def _write(self, incident_id: str, name: str, content: str, *, immutable: bool, content_type: str) -> str:
        from azure.storage.blob import ContentSettings

        blob_name = self._blob_name(incident_id, name)
        try:
            await self._container.upload_blob(
                blob_name,
                content.encode("utf-8"),
                overwrite=not immutable,
                content_settings=ContentSettings(content_type=content_type),
            )
        except ResourceExistsError as exc:
            raise EvidenceExistsError(f"Evidence artefact already exists and is immutable: {name}") from exc
        return f"{SCHEME}{incident_id}/{name}"

    async def put_json(self, incident_id: str, name: str, payload: Any, *, immutable: bool = True) -> str:
        content = json.dumps(redact(payload), indent=2, default=str, sort_keys=True)
        return await self._write(incident_id, name, content, immutable=immutable, content_type=_CONTENT_TYPE_JSON)

    async def put_text(self, incident_id: str, name: str, text: str, *, immutable: bool = False) -> str:
        return await self._write(incident_id, name, text, immutable=immutable, content_type=_CONTENT_TYPE_TEXT)

    async def get_json(self, uri: str) -> Any:
        return json.loads(await self.get_text(uri))

    async def get_text(self, uri: str) -> str:
        blob_name = self._from_uri(uri)
        try:
            downloader = await self._container.download_blob(blob_name)
        except ResourceNotFoundError as exc:
            raise FileNotFoundError(uri) from exc
        return (await downloader.readall()).decode("utf-8")

    async def incident_ids(self) -> list[str]:
        """Every incident that has evidence stored (the top-level folders under the path prefix)."""
        ids: list[str] = []
        async for item in self._container.walk_blobs(name_starts_with=self._prefix or None, delimiter="/"):
            name = getattr(item, "prefix", None)  # a folder; plain blobs next to the folders are not incidents
            if isinstance(name, str):
                ids.append(name[len(self._prefix) :].rstrip("/"))
        return sorted(ids)

    async def list(self, incident_id: str) -> list[str]:
        folder = self._blob_name(incident_id, "placeholder").rsplit("/", 1)[0] + "/"
        names: list[str] = []
        async for blob in self._container.list_blobs(name_starts_with=folder):
            leaf = blob.name[len(folder) :]
            if leaf and "/" not in leaf:
                names.append(f"{SCHEME}{incident_id}/{leaf}")
        return sorted(names)
