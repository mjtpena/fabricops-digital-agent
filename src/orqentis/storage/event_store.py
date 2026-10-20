"""Operational event store.

Target architecture: the PostgreSQL system of record (ADR-0008). ``LocalEventStore`` is the SQLite development
adapter; both satisfy :class:`EventStore`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
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


# Scope of a watermark that belongs to a whole collector rather than to one item it covers.
COLLECTOR_SCOPE = ""


@dataclass(frozen=True)
class SequencedEvent:
    """An event with its position in the store's total order of insertion."""

    sequence: int
    event: OperationalEvent


class EventStore(Protocol):
    async def append(self, events: Sequence[OperationalEvent]) -> list[OperationalEvent]:
        """Append events, ignoring duplicates by ``dedupe_key``. Returns the newly stored events."""
        ...

    async def query(self, query: EventQuery) -> list[OperationalEvent]: ...

    async def get(self, event_id: str) -> OperationalEvent | None: ...

    async def count(self) -> int: ...

    # ---- incremental collection -------------------------------------------------------------------------
    async def get_watermarks(self, collector: str) -> dict[str, datetime]:
        """Per scope (an item id, or ``COLLECTOR_SCOPE``): everything observed up to this instant was appended.

        A scope that has no entry was never collected.
        """
        ...

    async def advance_watermarks(self, collector: str, updates: Mapping[str, datetime], *, force: bool = False) -> None:
        """Move watermarks forward; a value older than the stored one is ignored unless ``force`` (clock repair)."""
        ...

    # ---- durable consumption ----------------------------------------------------------------------------
    async def read_after(self, position: int, *, limit: int = 500) -> list[SequencedEvent]:
        """Events after ``position`` (0 = from the start), in sequence order, at most ``limit``.

        **Contract: only a gap-free, committed prefix may be returned.** A consumer advances its cursor to the highest
        sequence it was given and never looks back, so if an event with a lower sequence could still become visible
        later, that event would be skipped forever. ``sequence`` must therefore increase with every inserted event and
        never be reused, *and* an implementation must not return an event while a lower-numbered one may still commit.

        * SQLite (``LocalEventStore``): one connection and a lock serialise writes, so ``rowid`` order is commit order
          and the contract holds.
        * PostgreSQL: a ``bigserial`` alone does **not** satisfy it. Values are assigned at insert time, not commit
          time, so concurrent transactions (one process with a connection pool is enough) can commit out of order.
          Serialise appends (``pg_advisory_xact_lock`` around the insert) or read only below the oldest in-flight
          transaction (``pg_snapshot_xmin(pg_current_snapshot())`` against the transaction id stored with each row).
        """
        ...

    async def get_cursor(self, consumer: str) -> int:
        """The last sequence the named consumer has fully processed (0 if it has not consumed anything)."""
        ...

    async def advance_cursor(self, consumer: str, position: int) -> int:
        """Record that ``consumer`` processed everything up to ``position``. Never moves backwards."""
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
    # How far each collector's observations are complete, per scope (an item id; '' for the whole collector).
    """CREATE TABLE IF NOT EXISTS watermarks (
        collector TEXT NOT NULL,
        scope TEXT NOT NULL,
        watermark REAL NOT NULL,
        PRIMARY KEY (collector, scope)
    )""",
    # How far each consumer (e.g. detection) has processed the event sequence.
    """CREATE TABLE IF NOT EXISTS consumer_cursors (
        consumer TEXT PRIMARY KEY,
        position INTEGER NOT NULL
    )""",
)


_ADVANCE_WATERMARK = (
    "INSERT INTO watermarks (collector, scope, watermark) VALUES (?, ?, ?) "
    "ON CONFLICT(collector, scope) DO UPDATE SET watermark = MAX(watermark, excluded.watermark)"
)
_FORCE_WATERMARK = (
    "INSERT INTO watermarks (collector, scope, watermark) VALUES (?, ?, ?) "
    "ON CONFLICT(collector, scope) DO UPDATE SET watermark = excluded.watermark"
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

    # ---- incremental collection -------------------------------------------------------------------------
    async def get_watermarks(self, collector: str) -> dict[str, datetime]:
        rows = await self._db.fetchall("SELECT scope, watermark FROM watermarks WHERE collector = ?", (collector,))
        return {str(r["scope"]): datetime.fromtimestamp(float(r["watermark"]), UTC) for r in rows}

    async def advance_watermarks(self, collector: str, updates: Mapping[str, datetime], *, force: bool = False) -> None:
        if not updates:
            return
        await self._db.executemany(
            _FORCE_WATERMARK if force else _ADVANCE_WATERMARK,
            [(collector, scope, value.timestamp()) for scope, value in updates.items()],
        )

    # ---- durable consumption ----------------------------------------------------------------------------
    async def read_after(self, position: int, *, limit: int = 500) -> list[SequencedEvent]:
        # rowid grows with every insert and is never reused while rows are only ever appended (never VACUUM this table
        # into a different order); INSERT OR IGNORE of a duplicate consumes none.
        rows = await self._db.fetchall(
            "SELECT rowid AS seq, body FROM operational_events WHERE rowid > ? ORDER BY rowid LIMIT ?",
            (position, limit),
        )
        return [SequencedEvent(int(r["seq"]), OperationalEvent.model_validate_json(r["body"])) for r in rows]

    async def get_cursor(self, consumer: str) -> int:
        row = await self._db.fetchone("SELECT position FROM consumer_cursors WHERE consumer = ?", (consumer,))
        return int(row["position"]) if row else 0

    async def advance_cursor(self, consumer: str, position: int) -> int:
        await self._db.execute(
            "INSERT INTO consumer_cursors (consumer, position) VALUES (?, ?) "
            "ON CONFLICT(consumer) DO UPDATE SET position = MAX(position, excluded.position)",
            (consumer, position),
        )
        return await self.get_cursor(consumer)
