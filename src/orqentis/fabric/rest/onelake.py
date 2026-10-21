"""Read-only OneLake (ADLS Gen2 API) access to a declared Delta table's ``_delta_log``.

Used by the schema-change collector (ADR-0009, ADR-0020). Two GETs only: list the log directory, and read the first
bytes of one commit file. The reader identity needs a OneLake data access role with ``Read`` on exactly the declared
table; workspace Viewer alone gets 403 on every path (docs/security.md section 11, proven live). Nothing here can
write: the only HTTP method used is ``GET``.
"""

from __future__ import annotations

import re
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any

from orqentis.domain.delta import DeltaCommitFile, DeltaCommitRef, DeltaTableRef
from orqentis.fabric.errors import FabricError
from orqentis.fabric.rest.http import FabricHttp
from orqentis.fabric.rest.util import segment
from orqentis.observability.logging import get_logger

_log = get_logger(__name__)

# The storage API version the live OneLake tests used (tests/live/test_live_onelake.py).
STORAGE_API_VERSION = "2023-11-03"
_COMMIT_FILE = re.compile(r"^(\d{20})\.json$")
_KEPT_ACTION = re.compile(r'^\s*\{\s*"(metaData|commitInfo|protocol)"\s*:')
_CONTENT_RANGE_TOTAL = re.compile(r"/(\d+)\s*$")

DEFAULT_MAX_COMMIT_BYTES = 2 * 1024 * 1024
_MAX_KEPT_LINES = 32
_LIST_PAGE_SIZE = 5000
_MAX_LIST_PAGES = 20


def _modified(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None


class RestDeltaLogService:
    """Implements :class:`~orqentis.fabric.ports.DeltaLogService` over the OneLake ADLS Gen2 endpoint."""

    def __init__(
        self,
        http: FabricHttp,
        *,
        base_url: str,
        scope: str,
        max_commit_bytes: int = DEFAULT_MAX_COMMIT_BYTES,
    ) -> None:
        if max_commit_bytes < 1024:
            raise ValueError("max_commit_bytes must be at least 1024")
        self._http = http
        self._base_url = base_url.rstrip("/")
        self._scope = scope
        self._max_commit_bytes = max_commit_bytes

    def _log_path(self, table: DeltaTableRef) -> str:
        return f"{table.item_id}/{table.path}/_delta_log"

    def _headers(self, **extra: str) -> dict[str, str]:
        return {"x-ms-version": STORAGE_API_VERSION, "Accept": "*/*", **extra}

    async def list_commits(self, table: DeltaTableRef) -> list[DeltaCommitRef]:
        url = f"{self._base_url}/{segment(table.workspace_id)}"
        params: dict[str, str | int] = {
            "resource": "filesystem",
            "recursive": "false",
            "directory": self._log_path(table),
            "maxResults": _LIST_PAGE_SIZE,
        }
        commits: dict[int, DeltaCommitRef] = {}
        for _ in range(_MAX_LIST_PAGES):
            response = await self._http.request("GET", url, scope=self._scope, params=params, headers=self._headers())
            try:
                body = response.json() if response.content else {}
            except ValueError as exc:
                raise FabricError(
                    "OneLake returned an unreadable directory listing", error_code="InvalidResponse"
                ) from exc
            paths = body.get("paths") if isinstance(body, dict) else None
            for entry in paths if isinstance(paths, list) else []:
                if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
                    continue
                if str(entry.get("isDirectory", "false")).lower() == "true":
                    continue
                match = _COMMIT_FILE.match(entry["name"].rsplit("/", 1)[-1])
                if match is not None:
                    version = int(match.group(1))
                    commits[version] = DeltaCommitRef(version=version, modified_at=_modified(entry.get("lastModified")))
            continuation = response.headers.get("x-ms-continuation")
            if not continuation:
                return [commits[v] for v in sorted(commits)]
            params = {**params, "continuation": continuation}
        _log.warning("delta log listing stopped: page limit reached", extra={"table_id": table.id})
        return [commits[v] for v in sorted(commits)]

    async def read_commit(self, table: DeltaTableRef, version: int) -> DeltaCommitFile:
        if version < 0:
            raise FabricError("Invalid Delta version", error_code="InvalidIdentifier")
        path = "/".join(
            segment(part) for part in (table.item_id, *table.path.split("/"), "_delta_log", f"{version:020d}.json")
        )
        url = f"{self._base_url}/{segment(table.workspace_id)}/{path}"
        cap = self._max_commit_bytes
        response = await self._http.request(
            "GET", url, scope=self._scope, headers=self._headers(Range=f"bytes=0-{cap - 1}")
        )
        data = response.content
        total = len(data)
        if (match := _CONTENT_RANGE_TOTAL.search(response.headers.get("Content-Range", ""))) is not None:
            total = int(match.group(1))
        truncated = total > cap or len(data) > cap
        text = data[:cap].decode("utf-8", errors="replace")
        lines = text.split("\n")
        if truncated and lines:
            lines = lines[:-1]  # the last line was cut mid-way
        kept = [line for line in lines if _KEPT_ACTION.match(line)][:_MAX_KEPT_LINES]
        if truncated:
            _log.warning(
                "delta commit larger than the read cap; later actions were not read",
                extra={"table_id": table.id, "version": version, "size_bytes": total, "cap_bytes": cap},
            )
        return DeltaCommitFile(version=version, lines=kept, truncated=truncated, size_bytes=total)
