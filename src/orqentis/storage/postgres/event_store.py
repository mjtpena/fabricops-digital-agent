"""PostgreSQL :class:`~orqentis.storage.event_store.EventStore`.

**The detection cursor needs a gap-free, committed prefix** (ADR-0013 section 7): ``read_after`` may return an event
only if no lower-numbered event can still become visible. A plain ``bigserial``/identity column does not give that:
numbers are assigned when the row is inserted, not when its transaction commits, so two concurrent appenders can commit
in the opposite order, and a consumer that has read past the higher number skips the lower one forever.

The choice made here (ADR-0017): **appends are serialised with a transaction-level advisory lock**
(``pg_advisory_xact_lock``) taken before the insert. Identity values are therefore assigned in lock order, and the lock
is released only after the transaction has committed *and become visible* (PostgreSQL releases locks after it has
removed the transaction from the in-progress set), so a reader that can see sequence ``n + 1`` can also see ``n``. A
transaction that rolls back leaves a hole in the numbering, which is harmless: the number was never visible.

Why not read only below the oldest in-flight transaction (``pg_snapshot_xmin``)? It would let appenders run in
parallel, but it makes the reader's correctness depend on every writer's transaction id being recorded and on long
transactions elsewhere in the database (which would stall detection). Event appends are small batches every few
minutes; serialising them costs nothing and keeps the argument one line long.

Readers take no lock. Duplicate events (``dedupe_key`` or ``event_id``) are skipped in the same statement.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from orqentis.domain.events import OperationalEvent
from orqentis.storage.event_store import EventQuery, SequencedEvent
from orqentis.storage.postgres.database import PostgresDatabase

# Arbitrary, fixed 64-bit key of the append lock ("ORQE" in ASCII).
_APPEND_LOCK_KEY = 0x4F52514E

_INSERT = """
INSERT INTO operational_events
    (event_id, dedupe_key, event_type, tenant_id, workspace_id, item_id, correlation_id, ts, body)
SELECT event_id, dedupe_key, event_type, tenant_id, workspace_id, item_id, correlation_id, ts, body
FROM unnest($1::text[], $2::text[], $3::text[], $4::text[], $5::text[], $6::text[], $7::text[], $8::timestamptz[],
            $9::text[])
     WITH ORDINALITY AS t(event_id, dedupe_key, event_type, tenant_id, workspace_id, item_id, correlation_id, ts,
                          body, ord)
ORDER BY ord
ON CONFLICT DO NOTHING
RETURNING event_id
"""

_ADVANCE_WATERMARK = (
    "INSERT INTO watermarks (collector, scope, watermark) VALUES ($1, $2, $3) "
    "ON CONFLICT (collector, scope) DO UPDATE SET watermark = GREATEST(watermarks.watermark, EXCLUDED.watermark)"
)
_FORCE_WATERMARK = (
    "INSERT INTO watermarks (collector, scope, watermark) VALUES ($1, $2, $3) "
    "ON CONFLICT (collector, scope) DO UPDATE SET watermark = EXCLUDED.watermark"
)


class PostgresEventStore:
    def __init__(self, db: PostgresDatabase) -> None:
        self._db = db

    async def append(self, events: Sequence[OperationalEvent]) -> list[OperationalEvent]:
        if not events:
            return []
        columns: list[list[Any]] = [[] for _ in range(9)]
        for event in events:
            for column, value in zip(
                columns,
                (
                    event.event_id,
                    event.dedupe_key,
                    event.event_type.value,
                    event.tenant_id,
                    event.workspace_id,
                    event.item_id,
                    event.correlation_id,
                    event.timestamp,
                    event.model_dump_json(),
                ),
                strict=True,
            ):
                column.append(value)
        async with self._db.transaction() as conn:
            # Everything that inserts into operational_events takes this lock first (and holds it until it commits).
            await conn.execute("SELECT pg_advisory_xact_lock($1)", _APPEND_LOCK_KEY)
            inserted = {row["event_id"] for row in await conn.fetch(_INSERT, *columns)}
        stored: list[OperationalEvent] = []
        for event in events:  # a duplicate inside the batch is returned once, like the SQLite adapter
            if event.event_id in inserted:
                inserted.discard(event.event_id)
                stored.append(event)
        return stored

    async def query(self, query: EventQuery) -> list[OperationalEvent]:
        clauses: list[str] = []
        args: list[Any] = []

        def add(template: str, value: Any) -> None:
            args.append(value)
            clauses.append(template.format(n=len(args)))

        for column, values in (
            ("item_id", query.item_ids),
            ("workspace_id", query.workspace_ids),
            ("event_type", [t.value for t in query.event_types] if query.event_types is not None else None),
        ):
            if values is not None:
                if not values:
                    return []
                add(f"{column} = ANY(${{n}}::text[])", list(values))
        if query.correlation_id is not None:
            add("correlation_id = ${n}", query.correlation_id)
        if query.since is not None:
            add("ts >= ${n}", query.since)
        if query.until is not None:
            add("ts <= ${n}", query.until)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        order = "DESC" if query.newest_first else "ASC"
        args.append(query.limit)
        rows = await self._db.fetch(
            f"SELECT body FROM operational_events {where} ORDER BY ts {order}, event_id LIMIT ${len(args)}",  # noqa: S608
            *args,
        )
        return [OperationalEvent.model_validate_json(r["body"]) for r in rows]

    async def get(self, event_id: str) -> OperationalEvent | None:
        row = await self._db.fetchrow("SELECT body FROM operational_events WHERE event_id = $1", event_id)
        return OperationalEvent.model_validate_json(row["body"]) if row else None

    async def count(self) -> int:
        return int(await self._db.fetchval("SELECT COUNT(*) FROM operational_events"))

    # ---- incremental collection -------------------------------------------------------------------------
    async def get_watermarks(self, collector: str) -> dict[str, datetime]:
        rows = await self._db.fetch("SELECT scope, watermark FROM watermarks WHERE collector = $1", collector)
        return {str(r["scope"]): r["watermark"] for r in rows}

    async def advance_watermarks(self, collector: str, updates: Mapping[str, datetime], *, force: bool = False) -> None:
        if not updates:
            return
        async with self._db.transaction() as conn:
            await conn.executemany(
                _FORCE_WATERMARK if force else _ADVANCE_WATERMARK,
                [(collector, scope, value) for scope, value in updates.items()],
            )

    # ---- durable consumption ----------------------------------------------------------------------------
    async def read_after(self, position: int, *, limit: int = 500) -> list[SequencedEvent]:
        # No lock: appends are serialised and each commits before the next can start, so what is visible is a prefix.
        rows = await self._db.fetch(
            "SELECT seq, body FROM operational_events WHERE seq > $1 ORDER BY seq LIMIT $2", position, limit
        )
        return [SequencedEvent(int(r["seq"]), OperationalEvent.model_validate_json(r["body"])) for r in rows]

    async def get_cursor(self, consumer: str) -> int:
        value = await self._db.fetchval("SELECT position FROM consumer_cursors WHERE consumer = $1", consumer)
        return int(value) if value is not None else 0

    async def advance_cursor(self, consumer: str, position: int) -> int:
        async with self._db.transaction() as conn:
            value = await conn.fetchval(
                "INSERT INTO consumer_cursors (consumer, position) VALUES ($1, $2) "
                "ON CONFLICT (consumer) DO UPDATE SET "
                "position = GREATEST(consumer_cursors.position, EXCLUDED.position) RETURNING position",
                consumer,
                position,
            )
        return int(value)
