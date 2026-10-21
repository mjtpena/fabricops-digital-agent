"""Capacity Metrics app adapter: CU utilisation and throttling through the Power BI ``executeQueries`` API.

Capacity CU utilisation has no public Fabric REST API (ADR-0009). The Microsoft Fabric Capacity Metrics app keeps it in
a semantic model, and ``POST /groups/{workspace}/datasets/{dataset}/executeQueries`` runs DAX against that model. This
adapter is read-only and sends nothing else.

Three things shape the code, and ``docs/spikes/capacity-metrics.md`` has the evidence for each:

* **The model is not an API.** Microsoft states that using it outside the app's own reports "isn't supported" and does
  not document its tables. The names in :class:`_Model` come from third-party write-ups of two app versions that already
  disagree, plus UI labels. Nothing here was run against a real tenant. Every column the queries are meant to return is
  therefore checked (``CapacityMetricsSchemaMismatch``), and a DAX error that says a name does not exist is reported the
  same way. The adapter fails closed; it never guesses a column.
* **The model has 30-second timepoints.** A week is 20,160 of them. They are read as one series and folded into
  ``BUCKET`` buckets (one snapshot per bucket), which keeps the event store small and each response far below the
  100,000-row limit of ``executeQueries``. Per-item usage is a per-timepoint drill-through in the app, so the top
  workloads are read at the peak timepoint of the busiest buckets only (:data:`MAX_WORKLOAD_QUERIES` requests at most).
* **The DAX has no parameters.** The capacity id and the timestamps are interpolated into the query text, so the
  capacity id must be a GUID and timestamps are produced from ``datetime`` integers, never from strings. Nothing the
  model returns (item names and kinds) is ever used in a later query.
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from orqentis.domain.common import ItemType
from orqentis.domain.runs import CapacitySnapshot, WorkloadUsage
from orqentis.fabric.errors import FabricCapabilityUnavailableError, FabricError
from orqentis.fabric.rest.auth import POWERBI_SCOPE
from orqentis.fabric.rest.http import FabricHttp, JsonObject, strip_privacy_tags
from orqentis.fabric.rest.util import segment
from orqentis.observability.logging import get_logger
from orqentis.tools.untrusted import sanitize_text

_log = get_logger(__name__)

BUCKET = timedelta(minutes=10)
MAX_WINDOW = timedelta(days=7)
MAX_WORKLOADS = 10  # top workloads kept per snapshot
MAX_WORKLOAD_QUERIES = 12  # item-level requests per call; throttled and busiest buckets first
# Below this a bucket is not worth an item-level request: a list of workloads at 3% CU explains nothing.
WORKLOAD_MIN_CU_PERCENT = 10.0
# The app's throttling charts are ratios to the 10-minute / 60-minute / 24-hour allowance: above 100% the capacity
# throttles, below it is overage protection. A snapshot reports throttling only when the threshold is exceeded.
THROTTLE_THRESHOLD_PERCENT = 100.0
MAX_NAME = 256

MODEL_UTC_OFFSET = timedelta(0)
_MAX_SERIES_ROWS = 21_000  # 7 days of 30-second timepoints is 20,160
_MAX_WORKLOAD_ROWS = 100
_MAX_UNTRUSTED_INPUT = MAX_NAME * 4  # text is cut to this before it is inspected
_MAX_DAX_ERROR = 300
_BIND_MODEL_PARAMETERS = True  # see docs/spikes/capacity-metrics.md, "MPARAMETER"

_GUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
# What a DAX error says when the model no longer has a table, column or measure the query names.
_UNRESOLVED_NAME = re.compile(
    r"failed to resolve name|wasn.t found|was not found|cannot find (?:table|name|column)|not a valid table",
    re.IGNORECASE,
)
_ROW_LIMIT = re.compile(r"more than \S+ rows", re.IGNORECASE)
_DROPPED_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})  # controls, bidi/zero-width, unassigned

_FABRIC_ITEM_TYPES = {
    t.value.lower(): t for t in ItemType if t not in (ItemType.DATA_PRODUCT, ItemType.EXTERNAL_SOURCE)
}
_ITEM_KIND_ALIASES = {"pipeline": ItemType.DATA_PIPELINE, "dataset": ItemType.SEMANTIC_MODEL}


class _Model:
    """Names in the Capacity Metrics semantic model: the weakest part of this adapter (the spike sources each one).

    Table and column names follow the app version 1031 (September 2025) as published by a third party; the
    capacity-level measures are UI labels used as placeholders and are assumptions. Probe them with
    ``EVALUATE TOPN(3, '<table>')`` on the real model (spike checklist, step 3) and change them here and nowhere else.
    """

    TIMEPOINTS, TIMEPOINT = "TimePoints", "TimePoint"
    CAPACITIES, CAPACITY_ID = "Capacities", "capacityId"
    ITEMS, WORKSPACE_ID, ITEM_ID, ITEM_NAME, ITEM_KIND = "Items", "Workspace Id", "Item Id", "Item name", "Item kind"
    INTERACTIVE_DETAIL, BACKGROUND_DETAIL = "Timepoint Interactive Detail", "Timepoint Background Detail"
    BASE_CAPACITY_PERCENT = "% of Base Capacity"
    CU_PERCENT, INTERACTIVE_DELAY, BACKGROUND_REJECTION = "CU %", "Interactive delay %", "Background rejection %"
    # Parameters the app's own visuals bind (``MPARAMETER``) when they read the detail tables: a list of capacity ids
    # and one timepoint. Named as in app version 1031; the 2024 write-up calls the first one ``CapacityID``.
    CAPACITY_PARAMETER, TIMEPOINT_PARAMETER = "CapacitiesList", "TimePoint"


# Aliases of the columns the queries create (the response names them ``[Alias]``).
_CU, _DELAY, _BACKGROUND = "CuPercent", "InteractiveDelayPercent", "BackgroundRejectionPercent"
_INTERACTIVE_CU, _BACKGROUND_CU = "InteractiveCuPercent", "BackgroundCuPercent"


def _column(table: str, name: str) -> str:
    return f"'{table}'[{name}]"


def _response_key(table: str | None, name: str) -> str:
    return f"{table}[{name}]" if table else f"[{name}]"


_SERIES_COLUMNS = (
    _response_key(_Model.TIMEPOINTS, _Model.TIMEPOINT),
    _response_key(None, _CU),
    _response_key(None, _DELAY),
    _response_key(None, _BACKGROUND),
)
_WORKLOAD_COLUMNS = (
    _response_key(_Model.ITEMS, _Model.WORKSPACE_ID),
    _response_key(_Model.ITEMS, _Model.ITEM_ID),
    _response_key(_Model.ITEMS, _Model.ITEM_NAME),
    _response_key(_Model.ITEMS, _Model.ITEM_KIND),
    _response_key(None, _INTERACTIVE_CU),
    _response_key(None, _BACKGROUND_CU),
)


# ----------------------------------------------------------------------------------------- DAX text
def _capacity_guid(value: str) -> str:
    """The capacity id as a lower-case GUID, or ``InvalidIdentifier``. Nothing else may reach the DAX text."""
    if not isinstance(value, str) or _GUID.fullmatch(value) is None:
        raise FabricError("The capacity id must be a GUID", error_code="InvalidIdentifier")
    return value.lower()


def _dax_string(value: str) -> str:
    """A DAX string literal; refuses (rather than escapes) quotes, control characters and anything non-ASCII."""
    if not value.isascii() or not value.isprintable() or '"' in value:
        raise FabricError("Refusing to put an unsafe value into a DAX query", error_code="InvalidIdentifier")
    return f'"{value}"'


def _dax_datetime(value: datetime) -> str:
    """A DAX date-time built from integers only (``DATE(y, m, d) + TIME(h, m, s)``), in the model's own clock."""
    return (
        f"(DATE({int(value.year)}, {int(value.month)}, {int(value.day)}) + "
        f"TIME({int(value.hour)}, {int(value.minute)}, {int(value.second)}))"
    )


def _series_query(capacity_id: str, start: datetime, end: datetime) -> str:
    """Capacity CU %, interactive delay % and background rejection % for every timepoint in ``[start, end]``."""
    timepoint = _column(_Model.TIMEPOINTS, _Model.TIMEPOINT)
    window = f"{timepoint} >= {_dax_datetime(start)} && {timepoint} <= {_dax_datetime(end)}"
    return "\n".join(
        [
            "EVALUATE",
            "TOPN(",
            f"    {_MAX_SERIES_ROWS},",
            "    SUMMARIZECOLUMNS(",
            f"        {timepoint},",
            f"        TREATAS({{{_dax_string(capacity_id)}}}, {_column(_Model.CAPACITIES, _Model.CAPACITY_ID)}),",
            f"        FILTER(ALL({timepoint}), {window}),",
            f'        "{_CU}", [{_Model.CU_PERCENT}],',
            f'        "{_DELAY}", [{_Model.INTERACTIVE_DELAY}],',
            f'        "{_BACKGROUND}", [{_Model.BACKGROUND_REJECTION}]',
            "    ),",
            f"    {timepoint}, DESC",
            ")",
            f"ORDER BY {timepoint} ASC",
        ]
    )


def _workload_query(capacity_id: str, timepoint: datetime) -> str:
    """The items that used the most CU in one timepoint (interactive plus background share of the base capacity)."""
    capacity, at = _dax_string(capacity_id), _dax_datetime(timepoint)
    lines = []
    if _BIND_MODEL_PARAMETERS:
        lines += [
            "DEFINE",
            f"    MPARAMETER '{_Model.CAPACITY_PARAMETER}' = {{ {capacity} }}",
            f"    MPARAMETER '{_Model.TIMEPOINT_PARAMETER}' = {at}",
        ]
    items = _Model.ITEMS
    lines += [
        "EVALUATE",
        "TOPN(",
        f"    {MAX_WORKLOADS},",
        "    SUMMARIZECOLUMNS(",
        f"        {_column(items, _Model.WORKSPACE_ID)},",
        f"        {_column(items, _Model.ITEM_ID)},",
        f"        {_column(items, _Model.ITEM_NAME)},",
        f"        {_column(items, _Model.ITEM_KIND)},",
        f"        TREATAS({{{capacity}}}, {_column(_Model.CAPACITIES, _Model.CAPACITY_ID)}),",
        f"        TREATAS({{{at}}}, {_column(_Model.TIMEPOINTS, _Model.TIMEPOINT)}),",
        f'        "{_INTERACTIVE_CU}", SUM({_column(_Model.INTERACTIVE_DETAIL, _Model.BASE_CAPACITY_PERCENT)}),',
        f'        "{_BACKGROUND_CU}", SUM({_column(_Model.BACKGROUND_DETAIL, _Model.BASE_CAPACITY_PERCENT)})',
        "    ),",
        f"    [{_INTERACTIVE_CU}] + [{_BACKGROUND_CU}], DESC",
        ")",
    ]
    return "\n".join(lines)


# ----------------------------------------------------------------------------- untrusted values
def _text(value: Any, limit: int = MAX_NAME) -> str:
    """One line of untrusted text: no control, bidi or zero-width characters, no envelope tags, at most ``limit``."""
    if value is None:
        return ""
    raw = value if isinstance(value, str) else str(value)
    spaced = "".join(" " if ch.isspace() else ch for ch in raw[:_MAX_UNTRUSTED_INPUT])
    kept = "".join(ch for ch in spaced if unicodedata.category(ch) not in _DROPPED_CATEGORIES)
    single_line = " ".join(kept.split())
    # A replaced envelope tag is longer than the tag, so sanitize_text must not truncate; the cut to ``limit`` is ours.
    return sanitize_text(single_line, max_length=_MAX_UNTRUSTED_INPUT * 4)[:limit].strip()


def _guid(value: Any) -> str | None:
    return value.lower() if isinstance(value, str) and _GUID.fullmatch(value) else None


def _item_type(kind: Any) -> ItemType:
    name = _text(kind, 64).lower()
    return _FABRIC_ITEM_TYPES.get(name) or _ITEM_KIND_ALIASES.get(name) or ItemType.OTHER


def _percent(value: Any) -> float | None:
    """A non-negative percentage; a blank (null) measure is 0, anything that is not a finite number is ``None``."""
    if value is None:
        return 0.0
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return max(number, 0.0) if math.isfinite(number) else None


def _normalise(key: str) -> str:
    return key.replace("'", "").strip().lower()


# --------------------------------------------------------------------------------------- time
def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _bucket_start(at: datetime) -> datetime:
    seconds, size = int(at.timestamp()), int(BUCKET.total_seconds())
    return datetime.fromtimestamp(seconds - seconds % size, UTC)


@dataclass(slots=True)
class _Bucket:
    start: datetime
    cu: float
    delay: float
    background: float
    peak_at: datetime  # the timepoint with the highest CU % (the earliest one when tied)
    peak_cu: float

    @property
    def throttling(self) -> bool:
        return self.delay > THROTTLE_THRESHOLD_PERCENT or self.background > THROTTLE_THRESHOLD_PERCENT


def _buckets(points: list[tuple[datetime, float, float, float]]) -> list[_Bucket]:
    """Fold timepoints ``(at, cu, delay, background_rejection)`` into one bucket per ``BUCKET``: the maxima."""
    folded: dict[datetime, _Bucket] = {}
    for at, cu, delay, background in sorted(points, key=lambda p: p[0]):
        start = _bucket_start(at)
        bucket = folded.get(start)
        if bucket is None:
            folded[start] = _Bucket(start, cu, delay, background, at, cu)
            continue
        bucket.cu, bucket.delay, bucket.background = (
            max(bucket.cu, cu),
            max(bucket.delay, delay),
            max(bucket.background, background),
        )
        if cu > bucket.peak_cu:
            bucket.peak_at, bucket.peak_cu = at, cu
    return [folded[start] for start in sorted(folded)]


def _workload_buckets(buckets: list[_Bucket]) -> list[_Bucket]:
    busy = [b for b in buckets if b.throttling or b.cu >= WORKLOAD_MIN_CU_PERCENT]
    busy.sort(key=lambda b: (not b.throttling, -b.cu, b.start))
    return busy[:MAX_WORKLOAD_QUERIES]


class CapacityMetricsReader:
    """CU utilisation, throttling and top workloads of a capacity, read from the Capacity Metrics semantic model.

    Implements ``CapacityService.get_capacity_metrics``. ``utc_offset`` is the app's ``UTC_offset`` parameter (the
    model's timepoints are in that clock, not UTC); the default of 0 is a **setup requirement** for the app (spike).
    """

    def __init__(self, http: FabricHttp, *, powerbi_base_url: str, workspace_id: str, dataset_id: str) -> None:
        self._http = http
        self._powerbi_base_url = powerbi_base_url
        self._workspace_id = workspace_id
        self._dataset_id = dataset_id
        self.utc_offset: timedelta = MODEL_UTC_OFFSET

    async def get_capacity_metrics(self, capacity_id: str, *, start: datetime, end: datetime) -> list[CapacitySnapshot]:
        """One snapshot per ``BUCKET`` (oldest first) with data in ``[start, end]``; the window is at most seven days.

        Raises ``FabricError(InvalidIdentifier)`` before any request when the capacity id is not a GUID,
        ``FabricCapabilityUnavailableError(CapacityMetricsSchemaMismatch)`` when the model no longer has what the
        queries need, and the HTTP layer's ``FabricAuthError`` when the caller may not query the model.
        """
        capacity = _capacity_guid(capacity_id)
        start, end = _utc(start).replace(microsecond=0), _utc(end).replace(microsecond=0)
        if end <= start:
            return []
        if end - start > MAX_WINDOW:
            _log.warning("capacity metrics window clamped", extra={"cm_requested_days": (end - start).days})
            start = end - MAX_WINDOW
        url = self._url()

        points = await self._series(url, capacity, start, end)
        buckets = _buckets(points)
        workloads = {b.start: await self._workloads(url, capacity, b) for b in _workload_buckets(buckets)}
        _log.info(
            "capacity metrics read",
            extra={"cm_timepoints": len(points), "cm_buckets": len(buckets), "cm_workload_queries": len(workloads)},
        )
        return [
            CapacitySnapshot(
                capacity_id=capacity,
                timestamp=b.start,
                cu_utilization_percent=round(b.cu, 2),
                interactive_delay_percent=round(b.delay, 2) if b.delay > THROTTLE_THRESHOLD_PERCENT else 0.0,
                background_rejection_percent=round(b.background, 2)
                if b.background > THROTTLE_THRESHOLD_PERCENT
                else 0.0,
                top_workloads=workloads.get(b.start, []),
            )
            for b in buckets
        ]

    # ----------------------------------------------------------------------------------- queries
    def _url(self) -> str:
        base = self._powerbi_base_url.rstrip("/")
        return f"{base}/groups/{segment(self._workspace_id)}/datasets/{segment(self._dataset_id)}/executeQueries"

    def _model_clock(self, value: datetime) -> datetime:
        return value + self.utc_offset

    def _from_model_clock(self, value: Any) -> datetime | None:
        """A timepoint from the response as UTC. The model gives naive date-times in its own clock."""
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value.strip())
        except ValueError:
            return None
        return parsed.astimezone(UTC) if parsed.tzinfo is not None else (parsed - self.utc_offset).replace(tzinfo=UTC)

    async def _series(
        self, url: str, capacity: str, start: datetime, end: datetime
    ) -> list[tuple[datetime, float, float, float]]:
        dax = _series_query(capacity, self._model_clock(start), self._model_clock(end))
        rows = await _execute(self._http, url, dax, phase="capacity", columns=_SERIES_COLUMNS, limit=_MAX_SERIES_ROWS)
        points: list[tuple[datetime, float, float, float]] = []
        for row in rows:
            values = {_normalise(k): v for k, v in row.items()}
            at = self._from_model_clock(values.get(_normalise(_SERIES_COLUMNS[0])))
            cu, delay, background = (_percent(values.get(_normalise(c))) for c in _SERIES_COLUMNS[1:])
            if at is None or cu is None or delay is None or background is None or not start <= at <= end:
                continue
            points.append((at, cu, delay, background))
        if rows and not points:
            raise _schema_mismatch("capacity", [], reason="no row had a readable timepoint and numeric measures")
        if len(points) < len(rows):
            _log.warning("capacity metrics rows skipped", extra={"cm_skipped_rows": len(rows) - len(points)})
        return points

    async def _workloads(self, url: str, capacity: str, bucket: _Bucket) -> list[WorkloadUsage]:
        dax = _workload_query(capacity, self._model_clock(bucket.peak_at))
        rows = await _execute(
            self._http, url, dax, phase="workloads", columns=_WORKLOAD_COLUMNS, limit=_MAX_WORKLOAD_ROWS
        )
        found: dict[str, WorkloadUsage] = {}
        skipped = 0
        for row in rows:
            values = {_normalise(k): v for k, v in row.items()}
            workspace_id, item_id = (_guid(values.get(_normalise(c))) for c in _WORKLOAD_COLUMNS[:2])
            interactive, background = (_percent(values.get(_normalise(c))) for c in _WORKLOAD_COLUMNS[4:])
            if workspace_id is None or item_id is None or interactive is None or background is None:
                skipped += 1  # virtual items and items the model has not seen yet carry no usable id
                continue
            cu = round(interactive + background, 2)
            if cu <= 0:
                continue
            current = found.get(item_id)
            if current is None or cu > current.cu_percent:
                found[item_id] = WorkloadUsage(
                    item_id=item_id,
                    workspace_id=workspace_id,
                    display_name=_text(values.get(_normalise(_WORKLOAD_COLUMNS[2]))) or "(unnamed item)",
                    item_type=_item_type(values.get(_normalise(_WORKLOAD_COLUMNS[3]))),
                    cu_percent=cu,
                )
        if skipped:
            _log.debug("capacity metrics workload rows skipped", extra={"cm_skipped_rows": skipped})
        return sorted(found.values(), key=lambda w: (-w.cu_percent, w.display_name))[:MAX_WORKLOADS]


# ------------------------------------------------------------------------- request and response
async def _execute(
    http: FabricHttp, url: str, dax: str, *, phase: str, columns: tuple[str, ...], limit: int
) -> list[JsonObject]:
    """Run one DAX query and return its rows, after the schema guard. ``phase`` names the query in errors."""
    body = {"queries": [{"query": dax}], "serializerSettings": {"includeNulls": True}}
    try:
        payload = await http.post_json(url, scope=POWERBI_SCOPE, json=body)
    except FabricError as exc:
        if exc.status_code == 400 and _UNRESOLVED_NAME.search(f"{exc.message} {json.dumps(exc.details, default=str)}"):
            raise _schema_mismatch(phase, [], reason=_pbi_message(exc)) from exc
        raise
    rows = _result_rows(payload, phase=phase)
    if len(rows) > limit:
        raise FabricError(
            f"The Capacity Metrics {phase} query returned {len(rows)} rows; at most {limit} are expected",
            error_code="CapacityMetricsRowLimit",
            details={"phase": phase, "limit": limit},
        )
    if rows:
        present = {_normalise(key) for row in rows for key in row}
        if missing := [name for name in columns if _normalise(name) not in present]:
            raise _schema_mismatch(phase, missing, found=sorted(present))
    return rows


def _result_rows(payload: JsonObject, *, phase: str) -> list[JsonObject]:
    """The rows of the single result table, or the error ``executeQueries`` reports inside a 200 response."""
    if isinstance(payload.get("error"), dict):
        raise _query_error(payload["error"], phase)
    results = payload.get("results")
    if not isinstance(results, list) or not results or not isinstance(results[0], dict):
        raise FabricError(
            "executeQueries returned no result for the Capacity Metrics query", error_code="InvalidResponse"
        )
    if isinstance(results[0].get("error"), dict):
        raise _query_error(results[0]["error"], phase)
    tables = results[0].get("tables")
    if not isinstance(tables, list) or not tables or not isinstance(tables[0], dict):
        raise FabricError(
            "executeQueries returned no table for the Capacity Metrics query", error_code="InvalidResponse"
        )
    if isinstance(tables[0].get("error"), dict):
        raise _query_error(tables[0]["error"], phase)
    rows = tables[0].get("rows")
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _query_error(error: JsonObject, phase: str) -> FabricError:
    """A DAX error that came back with HTTP 200 (too many rows, more than one table, a failed measure)."""
    message = _text(strip_privacy_tags(str(error.get("message") or "")), _MAX_DAX_ERROR)
    code = _text(error.get("code"), 64) or "DaxQueryError"
    if _UNRESOLVED_NAME.search(message):
        return _schema_mismatch(phase, [], reason=message)
    return FabricError(
        f"The Capacity Metrics {phase} query failed: {message or code}",
        error_code="CapacityMetricsRowLimit" if _ROW_LIMIT.search(message) else "DaxQueryError",
        details={"phase": phase, "dax_error_code": code},
    )


def _pbi_message(exc: FabricError) -> str:
    """The readable DAX error out of ``error.pbi.error.details[].detail.value`` (already redacted by the HTTP layer)."""
    pbi = exc.details.get("pbi_error")
    for entry in pbi.get("details", []) if isinstance(pbi, dict) else []:
        detail = entry.get("detail") if isinstance(entry, dict) else None
        if (
            isinstance(detail, dict)
            and isinstance(detail.get("value"), str)
            and not detail["value"].lstrip("-").isdigit()
        ):
            return _text(strip_privacy_tags(detail["value"]), _MAX_DAX_ERROR)
    return _text(exc.message, _MAX_DAX_ERROR)


def _schema_mismatch(
    phase: str, missing: list[str], *, found: list[str] | None = None, reason: str | None = None
) -> FabricCapabilityUnavailableError:
    details: dict[str, Any] = {"phase": phase, "missing_columns": missing}
    if found is not None:
        details["found_columns"] = [_text(name, 80) for name in found[:20]]
    if reason:
        details["reason"] = reason
    return FabricCapabilityUnavailableError(
        "The Capacity Metrics semantic model does not have what the capacity query needs "
        f"({phase}). The app was probably updated; see docs/spikes/capacity-metrics.md.",
        error_code="CapacityMetricsSchemaMismatch",
        details=details,
    )
