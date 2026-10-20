"""Incident persistence (document-style: the aggregate is stored as JSON plus indexed columns)."""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, Field

from orqentis.domain.incidents import Incident, IncidentStatus
from orqentis.storage.sqlite import SqliteDatabase


class DuplicateIncidentError(Exception):
    """The store already has an incident for this detection (same item and run, or same item and trigger event).

    Detection looks for an existing incident first and runs under a mutex, so this is the database's backstop for the
    moment the mutex is lost: a second pass must not open a second incident for one failure.
    """

    def __init__(self, existing: Incident | None) -> None:
        super().__init__("an incident already exists for this detection")
        self.existing = existing


class IncidentQuery(BaseModel):
    statuses: list[IncidentStatus] | None = None
    category: str | None = None
    primary_item_id: str | None = None
    workspace_id: str | None = None
    limit: int = Field(default=100, ge=1, le=1000)


class IncidentRepository(Protocol):
    async def next_incident_id(self, year: int) -> str: ...

    async def save(self, incident: Incident) -> None: ...

    async def get(self, incident_id: str) -> Incident | None: ...

    async def list(self, query: IncidentQuery | None = None) -> list[Incident]: ...

    async def find_by_correlation(self, primary_item_id: str, correlation_id: str) -> Incident | None: ...


_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS incidents (
        incident_id TEXT PRIMARY KEY,
        status TEXT NOT NULL,
        category TEXT NOT NULL,
        severity TEXT NOT NULL,
        primary_item_id TEXT NOT NULL,
        workspace_id TEXT NOT NULL,
        correlation_id TEXT,
        detected_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        body TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS ix_incidents_item ON incidents(primary_item_id, detected_at)",
)


class SqliteIncidentRepository:
    def __init__(self, db: SqliteDatabase) -> None:
        self._db = db
        db.migrate(_SCHEMA)

    async def next_incident_id(self, year: int) -> str:
        rows = await self._db.fetchall("SELECT incident_id FROM incidents WHERE incident_id LIKE ?", (f"INC-{year}-%",))
        highest = 0
        for row in rows:
            suffix = str(row["incident_id"]).rsplit("-", 1)[-1]
            if suffix.isdigit():
                highest = max(highest, int(suffix))
        return f"INC-{year}-{highest + 1:04d}"

    async def save(self, incident: Incident) -> None:
        await self._db.execute(
            "INSERT INTO incidents (incident_id, status, category, severity, primary_item_id, workspace_id, "
            "correlation_id, detected_at, updated_at, body) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(incident_id) DO UPDATE SET status=excluded.status, category=excluded.category, "
            "severity=excluded.severity, updated_at=excluded.updated_at, body=excluded.body",
            (
                incident.incident_id,
                incident.status.value,
                incident.category,
                incident.severity.value,
                incident.primary_item_id,
                incident.workspace_id,
                incident.correlation_id,
                incident.detected_at.timestamp(),
                incident.updated_at.timestamp(),
                incident.model_dump_json(),
            ),
        )

    async def get(self, incident_id: str) -> Incident | None:
        row = await self._db.fetchone("SELECT body FROM incidents WHERE incident_id = ?", (incident_id,))
        return Incident.model_validate_json(row["body"]) if row else None

    async def list(self, query: IncidentQuery | None = None) -> list[Incident]:
        q = query or IncidentQuery()
        clauses: list[str] = []
        params: list[object] = []
        if q.statuses:
            clauses.append(f"status IN ({','.join('?' for _ in q.statuses)})")
            params.extend(s.value for s in q.statuses)
        if q.category:
            clauses.append("category = ?")
            params.append(q.category)
        if q.primary_item_id:
            clauses.append("primary_item_id = ?")
            params.append(q.primary_item_id)
        if q.workspace_id:
            clauses.append("workspace_id = ?")
            params.append(q.workspace_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(q.limit)
        rows = await self._db.fetchall(
            f"SELECT body FROM incidents {where} ORDER BY detected_at DESC LIMIT ?",  # noqa: S608
            params,
        )
        return [Incident.model_validate_json(r["body"]) for r in rows]

    async def find_by_correlation(self, primary_item_id: str, correlation_id: str) -> Incident | None:
        row = await self._db.fetchone(
            "SELECT body FROM incidents WHERE primary_item_id = ? AND correlation_id = ? "
            "ORDER BY detected_at DESC LIMIT 1",
            (primary_item_id, correlation_id),
        )
        return Incident.model_validate_json(row["body"]) if row else None
