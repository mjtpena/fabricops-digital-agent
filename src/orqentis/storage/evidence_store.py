"""Evidence artefact store.

Investigation artefacts (raw tool payloads, reports, agent traces) outlive any agent conversation.
Target: OneLake. ``LocalEvidenceStore`` is the filesystem development adapter.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any, Protocol

from orqentis.observability.redaction import redact

_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SCHEME = "evidence://"


class EvidenceExistsError(Exception):
    pass


class EvidenceStore(Protocol):
    async def put_json(self, incident_id: str, name: str, payload: Any, *, immutable: bool = True) -> str: ...

    async def put_text(self, incident_id: str, name: str, text: str, *, immutable: bool = False) -> str: ...

    async def get_json(self, uri: str) -> Any: ...

    async def get_text(self, uri: str) -> str: ...

    async def list(self, incident_id: str) -> list[str]: ...

    def local_path(self, uri: str) -> Path | None: ...


class LocalEvidenceStore:
    def __init__(self, root: Path) -> None:
        self._root = root.resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, incident_id: str, name: str) -> Path:
        for segment in (incident_id, name):
            if not _SAFE_SEGMENT.match(segment) or ".." in segment:
                raise ValueError(f"Unsafe evidence path segment: {segment!r}")
        return self._root / incident_id / name

    def _from_uri(self, uri: str) -> Path:
        if not uri.startswith(SCHEME):
            raise ValueError(f"Not an evidence URI: {uri}")
        incident_id, _, name = uri[len(SCHEME) :].partition("/")
        return self._path(incident_id, name)

    def local_path(self, uri: str) -> Path | None:
        return self._from_uri(uri)

    def _write(self, path: Path, content: str, immutable: bool) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if immutable and path.exists():
            raise EvidenceExistsError(f"Evidence artefact already exists and is immutable: {path.name}")
        path.write_text(content, encoding="utf-8")

    async def put_json(self, incident_id: str, name: str, payload: Any, *, immutable: bool = True) -> str:
        path = self._path(incident_id, name)
        content = json.dumps(redact(payload), indent=2, default=str, sort_keys=True)
        await asyncio.to_thread(self._write, path, content, immutable)
        return f"{SCHEME}{incident_id}/{name}"

    async def put_text(self, incident_id: str, name: str, text: str, *, immutable: bool = False) -> str:
        path = self._path(incident_id, name)
        await asyncio.to_thread(self._write, path, text, immutable)
        return f"{SCHEME}{incident_id}/{name}"

    async def get_json(self, uri: str) -> Any:
        return json.loads(await self.get_text(uri))

    async def get_text(self, uri: str) -> str:
        path = self._from_uri(uri)
        return await asyncio.to_thread(path.read_text, encoding="utf-8")

    async def list(self, incident_id: str) -> list[str]:
        folder = self._path(incident_id, "placeholder").parent
        if not folder.exists():
            return []
        return sorted(f"{SCHEME}{incident_id}/{p.name}" for p in folder.iterdir() if p.is_file())
