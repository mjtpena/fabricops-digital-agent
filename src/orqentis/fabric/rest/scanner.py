"""Power BI admin Scanner API adapter: lineage for ``orqentis contracts scaffold --scanner``.

``POST /admin/workspaces/getInfo`` starts a metadata scan, ``GET .../scanStatus/{id}`` reports progress and
``GET .../scanResult/{id}`` returns the workspaces' reports, semantic models, upstream links and data
sources. It changes nothing in the tenant (the POST only asks the service to assemble metadata), but it
needs a Fabric administrator. It is therefore used only interactively, with a token from a signed-in
administrator (:class:`~orqentis.fabric.rest.auth.InteractiveTokenProvider`). The application runtime never
imports this module.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import Any
from urllib.parse import quote, urlsplit

from orqentis.contracts.lineage import LineageScan, ScannedDataset, ScannedDatasource, ScannedReport
from orqentis.fabric.errors import FabricError, FabricUnavailableError
from orqentis.fabric.rest.auth import POWERBI_SCOPE
from orqentis.fabric.rest.http import FabricHttp, JsonObject
from orqentis.observability.redaction import redact_text
from orqentis.tools.untrusted import sanitize_text

_BATCH_SIZE = 100  # getInfo accepts at most 100 workspaces per request
_SCAN_PARAMS = {"lineage": "True", "datasourceDetails": "True"}
_DETAIL_MAX = 200
_DETAIL_KEYS = ("server", "database", "url", "path", "sharePointSiteUrl", "account", "domain")


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _objects(value: Any) -> list[JsonObject]:
    return [entry for entry in value if isinstance(entry, dict)] if isinstance(value, list) else []


def _safe_detail(connection: Any) -> str:
    """A short, credential-free description of a data source (``server/database``, or a URL without query)."""
    if not isinstance(connection, dict):
        return ""
    parts: list[str] = []
    for key in _DETAIL_KEYS:
        value = _text(connection.get(key))
        if value is None:
            continue
        if "://" in value:
            split = urlsplit(value)
            value = f"{split.scheme}://{split.hostname or ''}{split.path}"  # drops userinfo, query, fragment
        parts.append(value)
    return sanitize_text(redact_text(" / ".join(parts)), max_length=_DETAIL_MAX)


class PowerBiScanner:
    """Implements :class:`~orqentis.contracts.lineage.LineageScanner` over the Power BI admin Scanner API."""

    def __init__(
        self,
        http: FabricHttp,
        powerbi_base_url: str,
        *,
        poll_interval_seconds: float = 3.0,
        max_wait_seconds: float = 600.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        on_close: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._http = http
        self._on_close = on_close
        self._base = powerbi_base_url.rstrip("/")
        self._poll_interval = poll_interval_seconds
        self._max_wait = max_wait_seconds
        self._sleep = sleep

    async def aclose(self) -> None:
        try:
            await self._http.aclose()
        finally:
            if self._on_close is not None:
                await self._on_close()

    def _url(self, *segments: str) -> str:
        return "/".join((self._base, "admin", "workspaces", *(quote(s, safe="") for s in segments)))

    async def scan(self, workspace_ids: Sequence[str]) -> LineageScan:
        reports: list[ScannedReport] = []
        datasets: list[ScannedDataset] = []
        datasources: dict[str, ScannedDatasource] = {}
        ids = list(dict.fromkeys(workspace_ids))
        for start in range(0, len(ids), _BATCH_SIZE):
            result = await self._scan_batch(ids[start : start + _BATCH_SIZE])
            for source in _objects(result.get("datasourceInstances")):
                if (source_id := _text(source.get("datasourceId"))) is not None:
                    datasources[source_id] = ScannedDatasource(
                        source_id,
                        sanitize_text(_text(source.get("datasourceType")) or "Unknown", max_length=60),
                        _safe_detail(source.get("connectionDetails")),
                    )
            for workspace in _objects(result.get("workspaces")):
                if (workspace_id := _text(workspace.get("id"))) is None:
                    continue
                for report in _objects(workspace.get("reports")):
                    if (report_id := _text(report.get("id"))) is not None:
                        reports.append(ScannedReport(report_id, workspace_id, _text(report.get("datasetId"))))
                for dataset in _objects(workspace.get("datasets")):
                    if (dataset_id := _text(dataset.get("id"))) is None:
                        continue
                    upstream = tuple(
                        t for u in _objects(dataset.get("upstreamDatasets")) if (t := _text(u.get("targetDatasetId")))
                    )
                    used = tuple(
                        s
                        for u in _objects(dataset.get("datasourceUsages"))
                        if (s := _text(u.get("datasourceInstanceId")))
                    )
                    datasets.append(ScannedDataset(dataset_id, workspace_id, upstream, used))
        return LineageScan(tuple(reports), tuple(datasets), tuple(datasources.values()))

    async def _scan_batch(self, workspace_ids: list[str]) -> JsonObject:
        started = await self._http.post_json(
            self._url("getInfo"), scope=POWERBI_SCOPE, params=_SCAN_PARAMS, json={"workspaces": workspace_ids}
        )
        scan_id = _text(started.get("id"))
        if scan_id is None:
            raise FabricError("The Scanner API did not return a scan id", error_code="InvalidResponse")
        waited = 0.0
        while True:
            status = await self._http.get_json(self._url("scanStatus", scan_id), scope=POWERBI_SCOPE)
            state = _text(status.get("status"))
            if state == "Succeeded":
                return await self._http.get_json(self._url("scanResult", scan_id), scope=POWERBI_SCOPE)
            if state not in (None, "NotStarted", "Running"):
                raise FabricError(
                    f"The metadata scan ended in state {sanitize_text(state, max_length=40)}",
                    error_code="ScanFailed",
                    details={"scan_id": scan_id},
                )
            if waited >= self._max_wait:
                raise FabricUnavailableError(
                    f"The metadata scan did not finish within {self._max_wait:.0f}s",
                    error_code="ScanTimeout",
                    details={"scan_id": scan_id},
                )
            await self._sleep(self._poll_interval)
            waited += self._poll_interval
