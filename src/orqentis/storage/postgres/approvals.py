"""PostgreSQL :class:`~orqentis.approvals.provider.ApprovalProvider` with compare-and-set transitions.

``transition`` is a single conditional ``UPDATE ... WHERE approval_id = $n AND revision = $m``. PostgreSQL serialises
two such updates of the same row: the second waits for the first transaction, re-evaluates its condition against the new
row, finds a different revision and updates nothing. So of any number of racing deciders, expiries, cancellations and
execution claims (in any number of processes or replicas) exactly one is stored, and everything that follows an
approval being ``EXECUTING`` happens at most once (ADR-0015).

Two further guarantees are enforced by the database rather than by the callers:

* **One pending approval per action.** A partial unique index on ``(incident_id, action_fingerprint)`` where the
  status is ``PENDING`` makes ``submit`` idempotent: asking again for an action that already has a pending request
  (a stalled worker and its replacement, two replicas) returns that request instead of creating a second one that
  could be approved and executed on its own.
* **Expiry by the database clock.** A decision or an execution claim made with ``enforce_expiry`` only succeeds while
  ``expires_at`` is in the future *by the database's clock* (the application's own clock when it is a scenario clock),
  inside the same statement that checks the revision. A replica whose clock lags cannot use an expired approval, and an
  approval that expires between the caller's check and the claim is not executed.
"""

from __future__ import annotations

from orqentis.config.clock import Clock
from orqentis.domain.approvals import ApprovalRequest, ApprovalStatus
from orqentis.observability.logging import get_logger
from orqentis.storage.postgres.database import PostgresDatabase

_log = get_logger(__name__)

_INSERT = (
    "INSERT INTO approvals (approval_id, incident_id, status, action_fingerprint, requested_at, revision, body, "
    "expires_at) VALUES ($1, $2, $3, $4, $5, $6, $7, $8) ON CONFLICT DO NOTHING RETURNING approval_id"
)


class PostgresApprovalProvider:
    def __init__(self, db: PostgresDatabase, clock: Clock | None = None, *, database_clock: bool = False) -> None:
        self._db = db
        self._clock = clock
        # Judge expiry with the database's clock (deployed environments). With a scenario clock (tests, demos) the
        # application's clock is the one that means something, so it is sent instead.
        self._database_clock = database_clock or clock is None

    async def submit(self, request: ApprovalRequest) -> ApprovalRequest:
        async with self._db.transaction() as conn:
            inserted = await conn.fetchrow(
                _INSERT,
                request.approval_id,
                request.incident_id,
                request.status.value,
                request.action_fingerprint,
                request.requested_at,
                request.revision,
                request.model_dump_json(),
                request.expires_at,
            )
            if inserted is None:
                pending = await conn.fetchrow(
                    "SELECT body FROM approvals WHERE incident_id = $1 AND action_fingerprint = $2 "
                    "AND status = 'PENDING'",
                    request.incident_id,
                    request.action_fingerprint,
                )
                if pending is not None and request.status is ApprovalStatus.PENDING:
                    existing = ApprovalRequest.model_validate_json(pending["body"])
                    _log.info(
                        "approval already pending for this action",
                        extra={"approval_id": existing.approval_id, "incident_id": existing.incident_id},
                    )
                    return existing
        if inserted is None:
            await self.save(request)  # the same approval id again: the initial write is an upsert, as before
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
                "body, expires_at) VALUES ($1, $2, $3, $4, $5, $6, $7, $8) ON CONFLICT (approval_id) DO UPDATE SET "
                "status = EXCLUDED.status, revision = EXCLUDED.revision, body = EXCLUDED.body, "
                "expires_at = EXCLUDED.expires_at",
                request.approval_id,
                request.incident_id,
                request.status.value,
                request.action_fingerprint,
                request.requested_at,
                request.revision,
                request.model_dump_json(),
                request.expires_at,
            )

    async def transition(
        self, updated: ApprovalRequest, *, expected_revision: int, enforce_expiry: bool = False
    ) -> ApprovalRequest | None:
        stored = updated.model_copy(update={"revision": expected_revision + 1})
        # $6 / $7: when asked, the approval must not have expired (database clock, or the scenario clock's reading).
        now = None if self._database_clock or self._clock is None else self._clock.now()
        async with self._db.transaction() as conn:
            row = await conn.fetchrow(
                "UPDATE approvals SET status = $1, revision = $2, body = $3 "
                "WHERE approval_id = $4 AND revision = $5 "
                "AND ($6::boolean IS NOT TRUE OR expires_at IS NULL OR expires_at > COALESCE($7::timestamptz, now())) "
                "RETURNING revision",
                stored.status.value,
                stored.revision,
                stored.model_dump_json(),
                stored.approval_id,
                expected_revision,
                enforce_expiry,
                now,
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
