"""Approval providers: where approval requests live and how approvers are notified.

``LocalApprovalProvider`` stores requests in SQLite and is driven through the REST API.
A future ``TeamsApprovalProvider`` implements the same port and posts Adaptive Cards whose
callbacks invoke :meth:`ApprovalService.decide`.
"""

from __future__ import annotations

from typing import Protocol

from orqentis.domain.approvals import ApprovalRequest, ApprovalStatus
from orqentis.observability.logging import get_logger
from orqentis.storage.sqlite import SqliteDatabase

_log = get_logger(__name__)


class ApprovalProvider(Protocol):
    async def submit(self, request: ApprovalRequest) -> ApprovalRequest: ...

    async def save(self, request: ApprovalRequest) -> None: ...

    async def get(self, approval_id: str) -> ApprovalRequest | None: ...

    async def list(
        self, *, incident_id: str | None = None, status: ApprovalStatus | None = None
    ) -> list[ApprovalRequest]: ...


_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS approvals (
        approval_id TEXT PRIMARY KEY,
        incident_id TEXT NOT NULL,
        status TEXT NOT NULL,
        action_fingerprint TEXT NOT NULL,
        requested_at REAL NOT NULL,
        body TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS ix_approvals_incident ON approvals(incident_id, status)",
)


class LocalApprovalProvider:
    def __init__(self, db: SqliteDatabase) -> None:
        self._db = db
        db.migrate(_SCHEMA)

    async def submit(self, request: ApprovalRequest) -> ApprovalRequest:
        await self.save(request)
        _log.info(
            "approval requested",
            extra={
                "approval_id": request.approval_id,
                "incident_id": request.incident_id,
                "required_role": request.required_role,
            },
        )
        return request

    async def save(self, request: ApprovalRequest) -> None:
        await self._db.execute(
            "INSERT INTO approvals (approval_id, incident_id, status, action_fingerprint, requested_at, body) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(approval_id) DO UPDATE SET status=excluded.status, "
            "body=excluded.body",
            (
                request.approval_id,
                request.incident_id,
                request.status.value,
                request.action_fingerprint,
                request.requested_at.timestamp(),
                request.model_dump_json(),
            ),
        )

    async def get(self, approval_id: str) -> ApprovalRequest | None:
        row = await self._db.fetchone("SELECT body FROM approvals WHERE approval_id = ?", (approval_id,))
        return ApprovalRequest.model_validate_json(row["body"]) if row else None

    async def list(
        self, *, incident_id: str | None = None, status: ApprovalStatus | None = None
    ) -> list[ApprovalRequest]:
        clauses: list[str] = []
        params: list[object] = []
        if incident_id:
            clauses.append("incident_id = ?")
            params.append(incident_id)
        if status:
            clauses.append("status = ?")
            params.append(status.value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = await self._db.fetchall(
            f"SELECT body FROM approvals {where} ORDER BY requested_at",  # noqa: S608
            params,
        )
        return [ApprovalRequest.model_validate_json(r["body"]) for r in rows]
