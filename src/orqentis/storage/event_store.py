"""Operational event store.

Target architecture: Microsoft Fabric Eventhouse (KQL database). ``LocalEventStore`` is the
SQLite development adapter; both satisfy :class:`EventStore`.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Protocol

from pydantic import BaseModel, Field

from orqentis.domain.events import EventType, OperationalEvent
from orqentis.storage.sqlite import SqliteDatabase


class EventQuery(BaseModel):
    item_ids: list[str] | None = None
    workspace_ids: list[str] | None = None
    event_types: list[EventType] | None = None
    correlation_id: str | None = None
    since: datetime | None = None
    until: datetime | None = None
    limit: int = Field(default=500, ge=1, le=10_000)
    newest_first: bool = False


class EventStore(Protocol):
    async def append(self, events: Sequence[OperationalEvent]) -> list[OperationalEvent]:
        """Append events, ignoring duplicates by ``dedupe_key``. Returns the newly stored events."""
        ...

    async def query(self, query: EventQuery) -> list[OperationalEvent]: ...

    async def get(self, event_id: str) -> OperationalEvent | None: ...

    async def count(self) -> int: ...

    async def get_watermark(self, collector: str) -> datetime | None:
        """Everything the collector observed up to this instant has been appended (``None``: never collected)."""
        ...

    async def advance_watermark(self, collector: str, watermark: datetime) -> datetime:
        """Move the collector's watermark forward. It never moves backwards. Returns the stored value."""
        ...


_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS operational_events (
        event_id TEXT PRIMARY KEY,
        dedupe_key TEXT NOT NULL UNIQUE,
        event_type TEXT NOT NULL,
        tenant_id TEXT NOT NULL,
        workspace_id TEXT,
        item_id TEXT,
        correlation_id TEXT,
        ts REAL NOT NULL,
        body TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS ix_events_item_ts ON operational_events(item_id, ts)",
    "CREATE INDEX IF NOT EXISTS ix_events_type_ts ON operational_events(event_type, ts)",
    # One row per collector: how far its observations are complete (incremental collection).
    """CREATE TABLE IF NOT EXISTS collector_watermarks (
        collector TEXT PRIMARY KEY,
        watermark REAL NOT NULL
    )""",
)


def _in_clause(column: str, values: Sequence[str], params: list[object]) -> str:
    params.extend(values)
    return f"{column} IN ({','.join('?' for _ in values)})"


class LocalEventStore:
    def __init__(self, db: SqliteDatabase) -> None:
        self._db = db
        db.migrate(_SCHEMA)

    async def append(self, events: Sequence[OperationalEvent]) -> list[OperationalEvent]:
        stored: list[OperationalEvent] = []
        for event in events:
            changed = await self._db.execute(
                "INSERT OR IGNORE INTO operational_events "
                "(event_id, dedupe_key, event_type, tenant_id, workspace_id, item_id, correlation_id, ts, body) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event.event_id,
                    event.dedupe_key,
                    event.event_type.value,
                    event.tenant_id,
                    event.workspace_id,
                    event.item_id,
                    event.correlation_id,
                    event.timestamp.timestamp(),
                    event.model_dump_json(),
                ),
            )
            if changed:
                stored.append(event)
        return stored

    async def query(self, query: EventQuery) -> list[OperationalEvent]:
        clauses: list[str] = []
        params: list[object] = []
        if query.item_ids is not None:
            if not query.item_ids:
                return []
            clauses.append(_in_clause("item_id", query.item_ids, params))
        if query.workspace_ids is not None:
            if not query.workspace_ids:
                return []
            clauses.append(_in_clause("workspace_id", query.workspace_ids, params))
        if query.event_types is not None:
            if not query.event_types:
                return []
            clauses.append(_in_clause("event_type", [t.value for t in query.event_types], params))
        if query.correlation_id is not None:
            clauses.append("correlation_id = ?")
            params.append(query.correlation_id)
        if query.since is not None:
            clauses.append("ts >= ?")
            params.append(query.since.timestamp())
        if query.until is not None:
            clauses.append("ts <= ?")
            params.append(query.until.timestamp())
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        order = "DESC" if query.newest_first else "ASC"
        params.append(query.limit)
        rows = await self._db.fetchall(
            f"SELECT body FROM operational_events {where} ORDER BY ts {order}, event_id LIMIT ?",  # noqa: S608
            params,
        )
        return [OperationalEvent.model_validate_json(r["body"]) for r in rows]

    async def get(self, event_id: str) -> OperationalEvent | None:
        row = await self._db.fetchone("SELECT body FROM operational_events WHERE event_id = ?", (event_id,))
        return OperationalEvent.model_validate_json(row["body"]) if row else None

    async def count(self) -> int:
        row = await self._db.fetchone("SELECT COUNT(*) AS n FROM operational_events")
        return int(row["n"]) if row else 0

    async def get_watermark(self, collector: str) -> datetime | None:
        row = await self._db.fetchone("SELECT watermark FROM collector_watermarks WHERE collector = ?", (collector,))
        return datetime.fromtimestamp(float(row["watermark"]), UTC) if row else None

    async def advance_watermark(self, collector: str, watermark: datetime) -> datetime:
        await self._db.execute(
            "INSERT INTO collector_watermarks (collector, watermark) VALUES (?, ?) "
            "ON CONFLICT(collector) DO UPDATE SET watermark = MAX(watermark, excluded.watermark)",
            (collector, watermark.timestamp()),
        )
        stored = await self.get_watermark(collector)
        assert stored is not None
        return stored
