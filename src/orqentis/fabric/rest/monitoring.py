"""Workspace monitoring adapter: semantic model refresh outcomes from the workspace's monitoring Eventhouse.

Why this exists: the Power BI refresh history needs dataset *Write*, which the reader identity must never hold
(docs/security.md section 11). Fabric workspace monitoring logs the Analysis Services engine events of every
semantic model of a workspace into a read-only KQL database; this adapter reads the refresh commands from there.
docs/spikes/workspace-monitoring.md has the evidence, and lists what could not be verified without a tenant.

Two calls, both read-only:

1. Fabric REST ``GET /workspaces/{id}/kqlDatabases`` finds the monitoring database and its ``queryServiceUri``
   (cached per workspace for a while; dropped on 403 and 404 so a re-created database is picked up).
2. ``POST {queryServiceUri}/v1/rest/query`` runs one static, parameterised KQL query (:data:`REFRESHES_KQL`) with the
   Kusto token audience. Nothing is ever formatted into the query text: ids and times travel as query parameters.

Everything the monitoring database returns is untrusted (anyone who can run a refresh writes the error text and the
model name): it is stripped of control and invisible characters, Power BI privacy markers are removed, lengths are
capped, ids must be GUIDs, and no returned value is used in a later request.
"""

from __future__ import annotations

import asyncio
import re
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from orqentis.domain.runs import FailureDetail, Refresh, RunStatus
from orqentis.fabric.errors import FabricAuthError, FabricCapabilityUnavailableError, FabricError, FabricNotFoundError
from orqentis.fabric.rest.auth import FABRIC_SCOPE
from orqentis.fabric.rest.http import FabricHttp, JsonObject, strip_privacy_tags
from orqentis.fabric.rest.util import clean, iso, parse_datetime, segment
from orqentis.observability.logging import get_logger

_log = get_logger(__name__)

SOURCE = "fabric.workspace-monitoring"
MAX_ROWS = 1000  # refreshes returned per call (newest first); more is logged as truncated
MAX_WINDOW = timedelta(days=31)  # monitoring keeps 30 days by default; older windows are clipped
_TARGET_TTL_SECONDS = 900.0
_QUERY_TIMEOUT = "00:00:30"  # servertimeout request property, a timespan

_MAX_MESSAGE = 2000
_MAX_NAME = 256
_MAX_LABEL = 64  # status, level and error code words

_GUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")

# One refresh = the engine commands that share an OperationId (the XMLA request id): a CommandBegin, and a CommandEnd
# that carries the outcome. A command is a refresh when its text says so (XMLA ``<Refresh xmlns...`` or TMSL
# ``{"refresh": ...}``), which is how Microsoft's own samples find refreshes; the match is made per operation, after
# grouping, so an End row without text is still counted. The error text is not on the CommandEnd: it is an ``Error``
# event of the same operation. Queries (QueryBegin/QueryEnd), discovers and other commands never enter the result.
#
# The text is static. The four query parameters are bound by the service (``properties.Parameters``); they are never
# formatted in. ``max_rows`` bounds the result, the window bounds the scan.
REFRESHES_KQL = """\
declare query_parameters(
    workspace_id: string,
    window_start: string,
    window_end: string,
    max_rows: long
);
let window_from = todatetime(window_start);
let window_to = todatetime(window_end);
let monitor_events = materialize(
    SemanticModelLogs
    | where Timestamp between ((window_from - 1d) .. window_to)
    | where WorkspaceId =~ workspace_id
    | where isnotempty(OperationId)
    | where OperationName in ('CommandBegin', 'CommandEnd', 'Error')
);
let refreshes = monitor_events
    | where OperationName in ('CommandBegin', 'CommandEnd')
    | summarize
        StartedAt = minif(Timestamp, OperationName == 'CommandBegin'),
        EndedAt = maxif(Timestamp, OperationName == 'CommandEnd'),
        ElapsedMs = maxif(DurationMs, OperationName == 'CommandEnd'),
        EndStatus = take_anyif(Status, OperationName == 'CommandEnd'),
        EndStatusCode = take_anyif(StatusCode, OperationName == 'CommandEnd'),
        EndLevel = take_anyif(Level, OperationName == 'CommandEnd'),
        Model = take_any(ItemId),
        ModelName = take_any(ItemName),
        Correlation = take_any(CorrelationId),
        RefreshHits = countif(EventText has_any ('<Refresh xmlns', '"refresh"'))
        by OperationId
    | where RefreshHits > 0;
let engine_errors = monitor_events
    | where OperationName == 'Error'
    | summarize arg_min(Timestamp, EventText, StatusCode) by OperationId
    | project OperationId, ErrorText = substring(EventText, 0, 4000), ErrorCode = tostring(StatusCode);
refreshes
| join kind=leftouter engine_errors on OperationId
| extend Began = coalesce(StartedAt, EndedAt - ElapsedMs * 1ms)
| extend SortTime = coalesce(EndedAt, Began)
| where SortTime between (window_from .. window_to)
| top max_rows by SortTime desc
| project
    refresh_id = OperationId,
    item_id = Model,
    item_name = ModelName,
    started_at = Began,
    ended_at = EndedAt,
    duration_ms = ElapsedMs,
    status = EndStatus,
    status_code = tostring(EndStatusCode),
    level = EndLevel,
    error_text = ErrorText,
    error_code = ErrorCode,
    correlation_id = Correlation
"""

_COLUMNS = (
    "refresh_id",
    "item_id",
    "item_name",
    "started_at",
    "ended_at",
    "duration_ms",
    "status",
    "status_code",
    "level",
    "error_text",
    "error_code",
    "correlation_id",
)

# Outcome vocabulary of the CommandEnd row. The documentation does not list the values of Status and Level (see the
# spike), so anything outside these sets is neither a success nor a failure unless an engine error was recorded.
_FAILED_WORDS = frozenset({"failed", "failure", "error", "faulted"})
_SUCCEEDED_WORDS = frozenset({"succeeded", "success", "successful", "completed", "ok"})
_CANCELLED_WORDS = frozenset({"cancelled", "canceled"})

_INVISIBLE_RANGES = ((0xFE00, 0xFE0F), (0xE0100, 0xE01EF))  # variation selectors can smuggle data in text
_DROPPED_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn"})  # controls, bidi and zero-width, tags, unassigned


def _safe_text(value: Any, limit: int) -> str | None:
    """Untrusted log text as one printable line: no control, bidi or invisible characters, no privacy markers."""
    if value is None:
        return None
    kept: list[str] = []
    for char in str(value)[: limit * 4 + 64]:  # bound the work a hostile value can cause
        if char.isspace():
            kept.append(" ")
        elif unicodedata.category(char) in _DROPPED_CATEGORIES or any(
            lo <= ord(char) <= hi for lo, hi in _INVISIBLE_RANGES
        ):
            continue
        else:
            kept.append(char)
    text = " ".join(strip_privacy_tags("".join(kept)).split())  # markers first, so none can be split by a dropped char
    return text[:limit].rstrip() or None


def _guid(value: Any) -> str | None:
    text = clean(value)
    return text.lower() if text is not None and _GUID.fullmatch(text) else None


def _require_guid(value: str, what: str) -> str:
    found = _guid(value)
    if found is None:
        raise FabricError(f"Invalid Fabric identifier for {what}", error_code="InvalidIdentifier")
    return found


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _outcome(*, ended: bool, status: str | None, level: str | None, error_text: str | None) -> RunStatus:
    if not ended:
        return RunStatus.IN_PROGRESS
    word, level_word = (status or "").casefold(), (level or "").casefold()
    if word in _FAILED_WORDS or level_word == "error":
        return RunStatus.FAILED
    if word in _SUCCEEDED_WORDS:
        return RunStatus.SUCCEEDED
    if word in _CANCELLED_WORDS:
        return RunStatus.CANCELLED
    if error_text:
        return RunStatus.FAILED  # the engine logged an error for this refresh and no status says otherwise
    return RunStatus.IN_PROGRESS  # unknown vocabulary: never reported as a success or a failure


def _newest_first(refresh: Refresh) -> tuple[float, str]:
    when = refresh.end_time or refresh.start_time
    return (when.timestamp() if when else 0.0, refresh.id)


def _table_missing(error: FabricError) -> bool:
    """Kusto answers 400 "failed to resolve ... SemanticModelLogs" while the table does not exist yet."""
    text = error.message.casefold()
    return error.status_code == 400 and "semanticmodellogs" in text and "resolve" in text


def _result_rows(body: JsonObject) -> list[dict[str, Any]]:
    """Rows of the result table of a Kusto v1 query response (found by its columns, not by its position)."""
    if exceptions := body.get("Exceptions"):
        first = exceptions[0] if isinstance(exceptions, list) and exceptions else exceptions
        raise FabricError(
            "Workspace monitoring query failed after it had started",
            error_code="QueryFailed",
            details={"error": _safe_text(first, 300)},
        )
    tables = body.get("Tables")
    for table in tables if isinstance(tables, list) else []:
        if not isinstance(table, dict):
            continue
        columns = table.get("Columns")
        names = [
            c["ColumnName"] if isinstance(c, dict) and isinstance(c.get("ColumnName"), str) else ""
            for c in columns or []
        ]
        if set(_COLUMNS) <= set(names):
            raw_rows = table.get("Rows")
            rows = raw_rows if isinstance(raw_rows, list) else []
            return [dict(zip(names, row, strict=False)) for row in rows[:MAX_ROWS] if isinstance(row, list)]
    raise FabricError("Workspace monitoring returned a result in an unexpected shape", error_code="InvalidResponse")


@dataclass(frozen=True)
class _Target:
    """Where the monitoring database of one workspace is queried."""

    query_uri: str
    database: str
    resolved_at: float


class RestMonitoringService:
    """Implements :class:`~orqentis.fabric.ports.MonitoringService` over Fabric workspace monitoring.

    ``database_name`` is the display name of the workspace's monitoring KQL database. ``kusto_scope`` is the token
    scope of the Eventhouse query endpoint (the Fabric scope is not accepted there).
    """

    def __init__(
        self, http: FabricHttp, *, fabric_base_url: str, tenant_id: str, database_name: str, kusto_scope: str
    ) -> None:
        self._http = http
        self._fabric_base_url = fabric_base_url.rstrip("/")
        self._tenant_id = tenant_id
        self._database_name = database_name.strip().casefold()
        self._kusto_scope = kusto_scope
        # A query endpoint is trusted only inside the Fabric API's parent domain (api.fabric.microsoft.com ->
        # *.fabric.microsoft.com): the bearer token goes to whatever host Fabric names, so it must be one of Fabric's.
        _, _, parent = (urlsplit(fabric_base_url).hostname or "").lower().partition(".")
        self._query_host_suffix = f".{parent}" if parent else ""
        self._targets: dict[str, _Target] = {}
        self._lock = asyncio.Lock()
        self._clock: Callable[[], float] = time.monotonic

    # ------------------------------------------------------------------------------------------ discovery
    def _trusted_query_uri(self, raw: Any) -> str | None:
        text = clean(raw)
        if text is None:
            return None
        try:
            parts = urlsplit(text)
            port = parts.port
        except ValueError:
            return None
        host = (parts.hostname or "").lower()
        trusted = bool(self._query_host_suffix) and host.endswith(self._query_host_suffix)
        clean_url = (
            parts.scheme == "https"
            and port in (None, 443)
            and not (parts.username or parts.password or parts.query or parts.fragment or parts.path.strip("/"))
        )
        return f"https://{host}" if trusted and clean_url else None

    async def _discover(self, workspace_id: str) -> _Target:
        url = "/".join((self._fabric_base_url, "workspaces", segment(workspace_id), "kqlDatabases"))
        found = [
            raw
            for raw in await self._http.paginate(url, scope=FABRIC_SCOPE)
            if (clean(raw.get("displayName")) or "").casefold() == self._database_name
        ]
        if not found:
            raise FabricCapabilityUnavailableError(
                "Workspace monitoring is not enabled for this workspace, or its monitoring database is not visible "
                "to the reader",
                error_code="WorkspaceMonitoringNotEnabled",
                details={"workspace_id": workspace_id},
            )
        if len(found) > 1:
            raise FabricCapabilityUnavailableError(
                "More than one KQL database carries the monitoring database name; refusing to pick one",
                error_code="WorkspaceMonitoringAmbiguous",
                details={"workspace_id": workspace_id},
            )
        database = found[0]
        properties = database.get("properties")
        if not (isinstance(properties, dict) and clean(properties.get("queryServiceUri"))):
            # The list documents ``properties``; if a response leaves them out, ask for the one database.
            item_id = _require_guid(str(database.get("id")), "KQL database")
            database = await self._http.get_json(
                "/".join((self._fabric_base_url, "workspaces", segment(workspace_id), "kqlDatabases", item_id)),
                scope=FABRIC_SCOPE,
            )
            properties = database.get("properties")
        query_uri = self._trusted_query_uri(properties.get("queryServiceUri") if isinstance(properties, dict) else None)
        name = clean(database.get("displayName"))  # the Kusto database name: the item's display name (unverified)
        if query_uri is None or name is None:
            raise FabricError(
                "Fabric returned no usable query endpoint for the monitoring database",
                error_code="InvalidResponse",
                details={"workspace_id": workspace_id},
            )
        return _Target(query_uri=query_uri, database=name, resolved_at=self._clock())

    async def _target(self, workspace_id: str, *, refresh: bool = False) -> _Target:
        async with self._lock:
            cached = self._targets.get(workspace_id)
            if cached is not None and not refresh and self._clock() - cached.resolved_at < _TARGET_TTL_SECONDS:
                return cached
            self._targets.pop(workspace_id, None)
            target = await self._discover(workspace_id)
            self._targets[workspace_id] = target
            return target

    # ----------------------------------------------------------------------------------------------- query
    async def _query(
        self, target: _Target, workspace_id: str, since: datetime, until: datetime
    ) -> list[dict[str, Any]]:
        body = {
            "db": target.database,
            "csl": REFRESHES_KQL,
            "properties": {
                "Options": {"request_readonly": True, "servertimeout": _QUERY_TIMEOUT},
                "Parameters": {
                    "workspace_id": workspace_id,
                    "window_start": iso(since),
                    "window_end": iso(until),
                    "max_rows": MAX_ROWS,
                },
            },
        }
        # POST, but a read-only query (request_readonly): it returns monitoring rows and changes nothing.
        response = await self._http.post_json(f"{target.query_uri}/v1/rest/query", scope=self._kusto_scope, json=body)
        return _result_rows(response)

    def _refresh(self, row: dict[str, Any], workspace_id: str) -> Refresh | None:
        refresh_id, item_id = _guid(row.get("refresh_id")), _guid(row.get("item_id"))
        if refresh_id is None or item_id is None:
            return None  # an id that is not a GUID was not written by the engine
        start, end = parse_datetime(row.get("started_at")), parse_datetime(row.get("ended_at"))
        status_word, level = _safe_text(row.get("status"), _MAX_LABEL), _safe_text(row.get("level"), _MAX_LABEL)
        status_code = _safe_text(row.get("status_code"), _MAX_LABEL)
        error_text = _safe_text(row.get("error_text"), _MAX_MESSAGE)
        status = _outcome(ended=end is not None, status=status_word, level=level, error_text=error_text)
        error: FailureDetail | None = None
        if status is RunStatus.FAILED:
            code = _safe_text(row.get("error_code"), _MAX_LABEL) or (status_code if status_code != "0" else None)
            error = FailureDetail(
                message=error_text or "Semantic model refresh failed; workspace monitoring recorded no error text",
                error_code=code,
            )
        metadata = {
            "monitoring_status": status_word,
            "monitoring_level": level,
            "monitoring_status_code": status_code,
            "item_name": _safe_text(row.get("item_name"), _MAX_NAME),
            "duration_ms": row.get("duration_ms") if isinstance(row.get("duration_ms"), int | float) else None,
        }
        return Refresh(
            id=refresh_id,  # the OperationId (XMLA request id): unique and stable, so a re-read dedupes
            tenant_id=self._tenant_id,
            workspace_id=workspace_id,
            item_id=item_id,
            refresh_type="Unknown",  # the engine log does not say who or what started the refresh
            status=status,
            start_time=start,
            end_time=end,
            error=error,
            correlation_id=_safe_text(row.get("correlation_id"), _MAX_LABEL) or refresh_id,
            source=SOURCE,
            metadata={k: v for k, v in metadata.items() if v is not None},
        )

    async def list_semantic_model_refreshes(
        self, workspace_id: str, *, since: datetime, until: datetime
    ) -> list[Refresh]:
        """Refreshes that ended (or, if unfinished, started) in ``[since, until]``, newest first.

        An unfinished refresh (a CommandBegin with no CommandEnd yet) is ``IN_PROGRESS``; so is a finished one whose
        outcome words are not recognised. Events reach the monitoring database a little after they happen, so a
        refresh that ended just before ``until`` can still show as unfinished.
        """
        workspace_id = _require_guid(workspace_id, "workspace")
        since, until = _utc(since), _utc(until)
        if until < since:
            return []
        since = max(since, until - MAX_WINDOW)
        rows: list[dict[str, Any]] = []
        for attempt in (1, 2):
            target = await self._target(workspace_id, refresh=attempt == 2)
            try:
                rows = await self._query(target, workspace_id, since, until)
                break
            except FabricAuthError:
                self._targets.pop(workspace_id, None)
                raise
            except FabricNotFoundError:
                self._targets.pop(workspace_id, None)  # the database was deleted or re-created: look it up once more
                if attempt == 2:
                    raise
            except FabricError as exc:
                if _table_missing(exc):
                    # The table appears only once a semantic model has produced telemetry: no table, no refreshes.
                    _log.info(
                        "workspace monitoring has no SemanticModelLogs table yet", extra={"workspace_id": workspace_id}
                    )
                    return []
                raise
        refreshes = [r for row in rows if (r := self._refresh(row, workspace_id)) is not None]
        if len(refreshes) < len(rows):
            _log.warning(
                "workspace monitoring rows dropped: ids are not GUIDs",
                extra={"workspace_id": workspace_id, "dropped": len(rows) - len(refreshes)},
            )
        if len(rows) >= MAX_ROWS:
            _log.warning(
                "workspace monitoring result truncated", extra={"workspace_id": workspace_id, "max_rows": MAX_ROWS}
            )
        refreshes.sort(key=_newest_first, reverse=True)
        return refreshes
