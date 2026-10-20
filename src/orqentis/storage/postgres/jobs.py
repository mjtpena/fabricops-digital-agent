"""PostgreSQL :class:`~orqentis.runtime.job_store.JobStore`: durable investigation jobs shared by every replica.

* **Idempotent per incident.** A partial unique index allows one ``QUEUED``/``RUNNING`` job per incident; ``enqueue``
  inserts with ``ON CONFLICT DO NOTHING`` and returns the existing active job instead.
* **Claims with ``FOR UPDATE SKIP LOCKED``.** Replicas poll; a claim picks the oldest queued job that no other
  transaction has locked, so concurrent workers never get the same job and never wait for each other.
* **Claim numbers.** Every claim increments ``claim_seq``; ``heartbeat``/``finish``/``release`` must present worker and
  claim number. A worker whose claim expired (a long stall) and was taken over cannot finish the job any more.
* **Expiry uses the database clock** (``now()``), like the lease.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import asyncpg

from orqentis.domain.principals import Principal
from orqentis.runtime.job_store import (
    ClaimedJob,
    InvestigationJob,
    InvestigationJobStatus,
    InvestigationOutcome,
    InvestigationTrigger,
)
from orqentis.storage.postgres.database import PostgresDatabase


def _job(row: asyncpg.Record) -> InvestigationJob:
    return InvestigationJob(
        job_id=row["job_id"],
        incident_id=row["incident_id"],
        status=InvestigationJobStatus(row["status"]),
        trigger=InvestigationTrigger(row["trigger"]),
        requested_by=row["requested_by"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        error=row["error"],
        result=InvestigationOutcome.model_validate_json(row["result"]) if row["result"] else None,
    )


class PostgresJobStore:
    shared = True

    def __init__(self, db: PostgresDatabase) -> None:
        self._db = db

    async def enqueue(self, job: InvestigationJob, principal: Principal) -> tuple[InvestigationJob, bool]:
        for _ in range(3):
            async with self._db.transaction() as conn:
                inserted = await conn.fetchrow(
                    "INSERT INTO investigation_jobs (job_id, incident_id, status, trigger, requested_by, principal, "
                    "created_at) VALUES ($1, $2, 'QUEUED', $3, $4, $5, $6) "
                    "ON CONFLICT (incident_id) WHERE status IN ('QUEUED', 'RUNNING') DO NOTHING RETURNING job_id",
                    job.job_id,
                    job.incident_id,
                    job.trigger.value,
                    job.requested_by,
                    principal.model_dump_json(),
                    job.created_at,
                )
                if inserted is not None:
                    return job, True
                existing = await conn.fetchrow(
                    "SELECT * FROM investigation_jobs WHERE incident_id = $1 AND status IN ('QUEUED', 'RUNNING')",
                    job.incident_id,
                )
            if existing is not None:
                return _job(existing), False
            # The active job finished between our failed insert and the lookup: try the insert again.
        raise RuntimeError(f"Could not enqueue or find an active investigation job for {job.incident_id}")

    async def get(self, job_id: str) -> InvestigationJob | None:
        row = await self._db.fetchrow("SELECT * FROM investigation_jobs WHERE job_id = $1", job_id)
        return _job(row) if row else None

    async def latest_for(self, incident_id: str) -> InvestigationJob | None:
        row = await self._db.fetchrow(
            "SELECT * FROM investigation_jobs WHERE incident_id = $1 ORDER BY created_at DESC, job_id DESC LIMIT 1",
            incident_id,
        )
        return _job(row) if row else None

    async def active_incident_ids(self) -> set[str]:
        rows = await self._db.fetch("SELECT incident_id FROM investigation_jobs WHERE status IN ('QUEUED', 'RUNNING')")
        return {r["incident_id"] for r in rows}

    async def is_active(self, incident_id: str) -> bool:
        return bool(
            await self._db.fetchval(
                "SELECT EXISTS (SELECT 1 FROM investigation_jobs WHERE incident_id = $1 "
                "AND status IN ('QUEUED', 'RUNNING'))",
                incident_id,
            )
        )

    async def count_failed_attempts(self, incident_id: str) -> int:
        return int(
            await self._db.fetchval(
                "SELECT COUNT(*) FROM investigation_jobs WHERE incident_id = $1 AND status = 'FAILED' "
                "AND counts_attempt AND created_at > COALESCE((SELECT MAX(finished_at) FROM investigation_jobs "
                "WHERE incident_id = $1 AND status = 'SUCCEEDED'), '-infinity'::timestamptz)",
                incident_id,
            )
        )

    async def claim_next(self, worker_id: str, claim_ttl: timedelta, started_at: datetime) -> ClaimedJob | None:
        async with self._db.transaction() as conn:
            row = await conn.fetchrow(
                "UPDATE investigation_jobs j SET status = 'RUNNING', claimed_by = $1, "
                "claim_expires_at = now() + $2::interval, claim_seq = j.claim_seq + 1, started_at = $3 "
                "WHERE j.job_id = (SELECT job_id FROM investigation_jobs WHERE status = 'QUEUED' "
                "ORDER BY created_at, job_id FOR UPDATE SKIP LOCKED LIMIT 1) "
                "RETURNING *",
                worker_id,
                claim_ttl,
                started_at,
            )
        if row is None:
            return None
        return ClaimedJob(
            job=_job(row),
            principal=Principal.model_validate_json(row["principal"]),
            claim=int(row["claim_seq"]),
            recovered=int(row["attempts"]) > 0,
        )

    async def heartbeat(self, job_id: str, worker_id: str, claim: int, claim_ttl: timedelta) -> bool:
        async with self._db.transaction() as conn:
            row = await conn.fetchrow(
                "UPDATE investigation_jobs SET claim_expires_at = now() + $4::interval "
                "WHERE job_id = $1 AND status = 'RUNNING' AND claimed_by = $2 AND claim_seq = $3 RETURNING job_id",
                job_id,
                worker_id,
                claim,
                claim_ttl,
            )
        return row is not None

    async def finish(
        self,
        job_id: str,
        worker_id: str,
        claim: int,
        *,
        status: InvestigationJobStatus,
        finished_at: datetime,
        error: str | None = None,
        result: InvestigationOutcome | None = None,
        counts_attempt: bool = True,
    ) -> bool:
        if status.is_active:
            raise ValueError("finish() needs a final status")
        async with self._db.transaction() as conn:
            row = await conn.fetchrow(
                "UPDATE investigation_jobs SET status = $4, finished_at = $5, error = $6, result = $7, "
                "counts_attempt = $8, claim_expires_at = NULL "
                "WHERE job_id = $1 AND status = 'RUNNING' AND claimed_by = $2 AND claim_seq = $3 RETURNING job_id",
                job_id,
                worker_id,
                claim,
                status.value,
                finished_at,
                error,
                result.model_dump_json() if result else None,
                counts_attempt,
            )
        return row is not None

    async def release(self, job_id: str, worker_id: str, claim: int) -> bool:
        async with self._db.transaction() as conn:
            row = await conn.fetchrow(
                "UPDATE investigation_jobs SET status = 'QUEUED', claimed_by = NULL, claim_expires_at = NULL, "
                "started_at = NULL WHERE job_id = $1 AND status = 'RUNNING' AND claimed_by = $2 AND claim_seq = $3 "
                "RETURNING job_id",
                job_id,
                worker_id,
                claim,
            )
        return row is not None

    async def reclaim_expired(self, *, max_attempts: int, finished_at: datetime) -> list[InvestigationJob]:
        async with self._db.transaction() as conn:
            rows = await conn.fetch(
                "WITH expired AS (SELECT job_id FROM investigation_jobs WHERE status = 'RUNNING' "
                "AND claim_expires_at < now() FOR UPDATE SKIP LOCKED) "
                "UPDATE investigation_jobs j SET attempts = j.attempts + 1, "
                "status = CASE WHEN j.attempts + 1 >= $1 THEN 'FAILED' ELSE 'QUEUED' END, "
                "claimed_by = NULL, claim_expires_at = NULL, "
                "started_at = CASE WHEN j.attempts + 1 >= $1 THEN j.started_at ELSE NULL END, "
                "finished_at = CASE WHEN j.attempts + 1 >= $1 THEN $2::timestamptz ELSE NULL END, "
                "error = CASE WHEN j.attempts + 1 >= $1 THEN 'Abandoned: ' || (j.attempts + 1) || "
                "' workers stopped answering while running this investigation' ELSE j.error END "
                "FROM expired WHERE j.job_id = expired.job_id RETURNING j.*",
                max_attempts,
                finished_at,
            )
        return [_job(r) for r in rows if r["status"] == InvestigationJobStatus.FAILED.value]

    async def cancel_queued(self, *, error: str, finished_at: datetime) -> list[InvestigationJob]:
        args: list[Any] = [error, finished_at]
        async with self._db.transaction() as conn:
            rows = await conn.fetch(
                "UPDATE investigation_jobs SET status = 'FAILED', error = $1, finished_at = $2, counts_attempt = false "
                "WHERE status = 'QUEUED' RETURNING *",
                *args,
            )
        return [_job(r) for r in rows]
