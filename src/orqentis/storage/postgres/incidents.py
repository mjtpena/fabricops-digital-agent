"""PostgreSQL :class:`~orqentis.storage.incidents.IncidentRepository` (the aggregate is stored as JSON text plus indexed
columns, exactly like the SQLite adapter).

Known limit (ADR-0017, follow-up): ``save`` is a last-writer-wins upsert, as it is in the SQLite adapter. Two replicas
that change the same incident at the same moment (a human deciding an approval while the leader adds a timeline note)
can lose one of the two changes. The window is small, the state the safety rules depend on (approvals, the audit
chain, leases, jobs) is protected by compare-and-set, locks and fencing instead, and a row version for the aggregate is
the planned fix.
"""

from __future__ import annotations

from typing import Any

import asyncpg

from orqentis.domain.incidents import Incident
from orqentis.storage.incidents import DuplicateIncidentError, IncidentQuery
from orqentis.storage.postgres.database import PostgresDatabase

_DETECTION_INDEX = "ux_incidents_detection"


def detection_key(incident: Incident) -> str | None:
    """The identity of the detection that opened the incident: the item and the failing run, else the item and event.

    Only incidents opened by detection have one (``metadata["detection_rule"]``): incidents loaded from history, or
    saved by hand, are not claims on a detection and may share a run id.
    """
    if "detection_rule" not in incident.metadata:
        return None
    if incident.correlation_id:
        return f"{incident.primary_item_id}|run|{incident.correlation_id}"
    trigger = incident.metadata.get("trigger_event_id")
    return f"{incident.primary_item_id}|event|{trigger}" if trigger else None


# Seeds the counter from the highest number already stored (history loaded from fixtures, a migrated database), then
# takes the next one. Both statements run in one transaction, and the UPDATE locks the counter row, so two replicas
# can never be given the same number.
_SEED_COUNTER = """
INSERT INTO incident_counters (year, last_value)
SELECT $1::integer, COALESCE(MAX(substring(incident_id FROM '([0-9]+)$')::integer), 0)
FROM incidents WHERE incident_id LIKE $2
ON CONFLICT (year) DO UPDATE SET last_value = GREATEST(incident_counters.last_value, EXCLUDED.last_value)
"""


class PostgresIncidentRepository:
    def __init__(self, db: PostgresDatabase) -> None:
        self._db = db

    async def next_incident_id(self, year: int) -> str:
        async with self._db.transaction() as conn:
            await conn.execute(_SEED_COUNTER, year, f"INC-{year}-%")
            number = await conn.fetchval(
                "UPDATE incident_counters SET last_value = last_value + 1 WHERE year = $1 RETURNING last_value", year
            )
        return f"INC-{year}-{int(number):04d}"

    async def raise_counters(self, highest: dict[int, int]) -> dict[int, int]:
        """Make sure the next number of each year is above ``highest[year]``; returns the counters that moved.

        After a point-in-time restore the counters can be behind the evidence that survived in Blob storage (evidence
        is not part of the database backup). ``orqentis db reseed-counters`` calls this with the highest incident
        numbers found there, so that no new incident reuses the identity of one whose evidence still exists.
        """
        moved: dict[int, int] = {}
        async with self._db.transaction() as conn:
            for year, number in sorted(highest.items()):
                row = await conn.fetchrow(
                    "INSERT INTO incident_counters (year, last_value) VALUES ($1, $2) "
                    "ON CONFLICT (year) DO UPDATE SET last_value = GREATEST(incident_counters.last_value, "
                    "EXCLUDED.last_value) WHERE incident_counters.last_value < EXCLUDED.last_value "
                    "RETURNING last_value",
                    year,
                    number,
                )
                if row is not None:
                    moved[year] = int(row["last_value"])
        return moved

    async def save(self, incident: Incident) -> None:
        key = detection_key(incident)
        try:
            async with self._db.transaction() as conn:
                await conn.execute(
                    "INSERT INTO incidents (incident_id, status, category, severity, primary_item_id, workspace_id, "
                    "correlation_id, detected_at, updated_at, body, detection_key) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11) "
                    "ON CONFLICT (incident_id) DO UPDATE SET status = EXCLUDED.status, category = EXCLUDED.category, "
                    "severity = EXCLUDED.severity, updated_at = EXCLUDED.updated_at, body = EXCLUDED.body",
                    incident.incident_id,
                    incident.status.value,
                    incident.category,
                    incident.severity.value,
                    incident.primary_item_id,
                    incident.workspace_id,
                    incident.correlation_id,
                    incident.detected_at,
                    incident.updated_at,
                    incident.model_dump_json(),
                    key,
                )
        except asyncpg.UniqueViolationError as exc:
            if exc.constraint_name != _DETECTION_INDEX or key is None:
                raise
            row = await self._db.fetchrow("SELECT body FROM incidents WHERE detection_key = $1", key)
            raise DuplicateIncidentError(Incident.model_validate_json(row["body"]) if row else None) from None

    async def get(self, incident_id: str) -> Incident | None:
        row = await self._db.fetchrow("SELECT body FROM incidents WHERE incident_id = $1", incident_id)
        return Incident.model_validate_json(row["body"]) if row else None

    async def list(self, query: IncidentQuery | None = None) -> list[Incident]:
        q = query or IncidentQuery()
        clauses: list[str] = []
        args: list[Any] = []
        if q.statuses:
            args.append([s.value for s in q.statuses])
            clauses.append(f"status = ANY(${len(args)}::text[])")
        for column, value in (
            ("category", q.category),
            ("primary_item_id", q.primary_item_id),
            ("workspace_id", q.workspace_id),
        ):
            if value:
                args.append(value)
                clauses.append(f"{column} = ${len(args)}")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        args.append(q.limit)
        rows = await self._db.fetch(
            f"SELECT body FROM incidents {where} ORDER BY detected_at DESC, incident_id DESC LIMIT ${len(args)}",  # noqa: S608
            *args,
        )
        return [Incident.model_validate_json(r["body"]) for r in rows]

    async def find_by_correlation(self, primary_item_id: str, correlation_id: str) -> Incident | None:
        row = await self._db.fetchrow(
            "SELECT body FROM incidents WHERE primary_item_id = $1 AND correlation_id = $2 "
            "ORDER BY detected_at DESC LIMIT 1",
            primary_item_id,
            correlation_id,
        )
        return Incident.model_validate_json(row["body"]) if row else None
