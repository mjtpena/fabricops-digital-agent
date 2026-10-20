"""PostgreSQL :class:`~orqentis.approvals.provider.ApprovalProvider` with compare-and-set transitions.

``transition`` is a single conditional ``UPDATE ... WHERE approval_id = $n AND revision = $m``. PostgreSQL serialises
two such updates of the same row: the second waits for the first transaction, re-evaluates its condition against the new
row, finds a different revision and updates nothing. So of any number of racing deciders, expiries, cancellations and
execution claims (in any number of processes or replicas) exactly one is stored, and everything that follows an
approval being ``EXECUTING`` happens at most once (ADR-0015).
"""

from __future__ import annotations

from orqentis.domain.approvals import ApprovalRequest, ApprovalStatus
from orqentis.observability.logging import get_logger
from orqentis.storage.postgres.database import PostgresDatabase

_log = get_logger(__name__)


class PostgresApprovalProvider:
    def __init__(self, db: PostgresDatabase) -> None:
        self._db = db

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
        async with self._db.transaction() as conn:
            await conn.execute(
                "INSERT INTO approvals (approval_id, incident_id, status, action_fingerprint, requested_at, revision, "
                "body) VALUES ($1, $2, $3, $4, $5, $6, $7) ON CONFLICT (approval_id) DO UPDATE SET "
                "status = EXCLUDED.status, revision = EXCLUDED.revision, body = EXCLUDED.body",
                request.approval_id,
                request.incident_id,
                request.status.value,
                request.action_fingerprint,
                request.requested_at,
                request.revision,
                request.model_dump_json(),
            )

    async def transition(self, updated: ApprovalRequest, *, expected_revision: int) -> ApprovalRequest | None:
        stored = updated.model_copy(update={"revision": expected_revision + 1})
        async with self._db.transaction() as conn:
            row = await conn.fetchrow(
                "UPDATE approvals SET status = $1, revision = $2, body = $3 "
                "WHERE approval_id = $4 AND revision = $5 RETURNING revision",
                stored.status.value,
                stored.revision,
                stored.model_dump_json(),
                stored.approval_id,
                expected_revision,
            )
        return stored if row is not None else None

    async def get(self, approval_id: str) -> ApprovalRequest | None:
        row = await self._db.fetchrow("SELECT body FROM approvals WHERE approval_id = $1", approval_id)
        return ApprovalRequest.model_validate_json(row["body"]) if row else None

    async def list(
        self, *, incident_id: str | None = None, status: ApprovalStatus | None = None
    ) -> list[ApprovalRequest]:
        clauses: list[str] = []
        args: list[object] = []
        if incident_id:
            args.append(incident_id)
            clauses.append(f"incident_id = ${len(args)}")
        if status:
            args.append(status.value)
            clauses.append(f"status = ${len(args)}")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = await self._db.fetch(
            f"SELECT body FROM approvals {where} ORDER BY requested_at, approval_id",  # noqa: S608
            *args,
        )
        return [ApprovalRequest.model_validate_json(r["body"]) for r in rows]
