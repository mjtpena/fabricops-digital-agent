"""Replay recorded Fabric / Power BI responses through an ``httpx`` mock transport, refusing anything that writes.

The recordings in ``tests/data/recorded`` were authored from the documented response shapes; no tenant was
contacted. Requests are matched on method and path. Several interactions with the same method and path are
served in order and the last one repeats (so pagination and "Running, then Succeeded" polling both work).
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

import httpx

DATA = Path(__file__).resolve().parents[1] / "data" / "recorded"

# POST endpoints that only *read*: the pipeline activity-run query, and (in scanner mode) the Scanner's getInfo.
READ_ONLY_POSTS = ("/queryactivityruns",)
SCANNER_POSTS = ("/admin/workspaces/getInfo",)


class RecordedTransport:
    def __init__(self, recording: str = "scaffold_estate.json", *, allow_scanner: bool = False) -> None:
        document = json.loads((DATA / recording).read_text(encoding="utf-8"))
        self._by_key: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for interaction in document["interactions"]:
            self._by_key[(interaction["method"], interaction["path"])].append(interaction)
        self._served: dict[tuple[str, str], int] = defaultdict(int)
        self._allowed_posts = READ_ONLY_POSTS + (SCANNER_POSTS if allow_scanner else ())
        self.requests: list[httpx.Request] = []
        self.rejected: list[str] = []  # writes (or other non-GET calls) that were refused
        self.unexpected: list[str] = []  # reads with no recording

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        method, path = request.method, request.url.path
        if method != "GET" and not any(path.endswith(suffix) for suffix in self._allowed_posts):
            self.rejected.append(f"{method} {path}")
            return httpx.Response(
                405, json={"errorCode": "MethodNotAllowed", "message": "recorded transport is read-only"}
            )
        candidates = self._by_key.get((method, path))
        if not candidates:
            self.unexpected.append(f"{method} {path}")
            return httpx.Response(404, json={"errorCode": "NotFound", "message": f"no recording for {method} {path}"})
        index = min(self._served[(method, path)], len(candidates) - 1)
        self._served[(method, path)] += 1
        interaction = candidates[index]
        return httpx.Response(interaction["status"], json=interaction["body"], headers=interaction.get("headers"))

    def calls(self, method: str, suffix: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method and r.url.path.endswith(suffix)]

    @property
    def methods(self) -> set[str]:
        return {r.method for r in self.requests}
