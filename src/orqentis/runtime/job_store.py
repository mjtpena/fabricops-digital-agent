"""Where investigation jobs live: a port with an in-memory adapter (local) and a PostgreSQL adapter (deployed).

A job moves ``QUEUED -> RUNNING -> SUCCEEDED | FAILED``. The store enforces what must hold however many processes share
it:

* **Idempotent submission.** At most one job per incident is queued or running; submitting again returns that job.
* **Claims.** ``claim_next`` hands a queued job to exactly one worker. The claim carries a number (``claim``) that only
  grows; ``heartbeat``, ``finish`` and ``release`` take it, so a worker whose claim was taken over after a stall cannot
  overwrite the new worker's job.
* **Expiry.** A claim that is not renewed (a crashed replica) can be reclaimed: the job is queued again, or failed after
  too many abandoned claims so that a job which kills its worker cannot loop forever.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from orqentis.config.clock import Clock
from orqentis.domain.incidents import IncidentStatus
from orqentis.domain.principals import Principal


class InvestigationJobNotFoundError(Exception):
    pass


class InvestigationJobStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"

    @property
    def is_active(self) -> bool:
        return self in {InvestigationJobStatus.QUEUED, InvestigationJobStatus.RUNNING}


class InvestigationTrigger(StrEnum):
    API = "API"
    SCHEDULER = "SCHEDULER"


class InvestigationOutcome(BaseModel):
    """Summary of a finished investigation (the full result is the incident itself)."""

    model_config = ConfigDict(extra="forbid")

    incident_status: IncidentStatus
    trace_id: str
    findings: int
    evidence: int
    tool_calls: int
    approval_ids: list[str] = Field(default_factory=list)
    confidence_level: str | None = None
    confidence_score: float | None = None


class InvestigationJob(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str
    incident_id: str
    status: InvestigationJobStatus
    trigger: InvestigationTrigger
    requested_by: str
    created_at: AwareDatetime
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None
    error: str | None = None
    result: InvestigationOutcome | None = None


@dataclass(frozen=True)
class ClaimedJob:
    job: InvestigationJob
    # The identity the job was submitted under (the investigation runs as it).
    principal: Principal
    # Identifies this claim; pass it back to heartbeat / finish / release.
    claim: int
    # True when an earlier worker abandoned the job (its claim expired), so the incident may be stuck INVESTIGATING.
    recovered: bool


class JobStore(Protocol):
    @property
    def shared(self) -> bool:
        """True when other processes use the same store (the runner then polls and heartbeats)."""
        ...

    async def enqueue(self, job: InvestigationJob, principal: Principal) -> tuple[InvestigationJob, bool]:
        """Store a queued job, or return the incident's active one (``created`` False)."""
        ...

    async def get(self, job_id: str) -> InvestigationJob | None: ...

    async def latest_for(self, incident_id: str) -> InvestigationJob | None: ...

    async def active_incident_ids(self) -> set[str]: ...

    async def is_active(self, incident_id: str) -> bool: ...

    async def count_failed_attempts(self, incident_id: str) -> int:
        """Failed jobs of the incident that count as an attempt, since its last success."""
        ...

    async def claim_next(self, worker_id: str, claim_ttl: timedelta, started_at: datetime) -> ClaimedJob | None: ...

    async def heartbeat(self, job_id: str, worker_id: str, claim: int, claim_ttl: timedelta) -> bool:
        """Extend the claim; False when it was lost (expired and taken over, or the job is no longer running)."""
        ...

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
        """Record the end of a claimed job; False when the claim is no longer ours (nothing is written)."""
        ...

    async def release(self, job_id: str, worker_id: str, claim: int) -> bool:
        """Give a claimed job back to the queue without counting an attempt (a graceful shutdown)."""
        ...

    async def release_claims(self, worker_id: str) -> int:
        """Give back every job this worker still holds, without counting an attempt; returns how many.

        For a graceful shutdown, after the worker's own tasks have ended: a claim that committed as the worker loop was
        cancelled belongs to no task, and would otherwise sit ``RUNNING`` until its claim expires.
        """
        ...

    async def reclaim_expired(
        self, *, max_attempts: int, finished_at: datetime, exclude: Collection[str] = ()
    ) -> list[InvestigationJob]:
        """Re-queue running jobs whose claim expired; returns the jobs that were failed for good instead.

        ``exclude`` are job ids the caller is itself running: a worker that could not heartbeat for a while (a database
        outage) must not take back, and start a second time, a job whose task is still alive. Another worker may.
        """
        ...

    async def cancel_queued(self, *, error: str, finished_at: datetime) -> list[InvestigationJob]:
        """Fail every job that is still queued, without counting an attempt.

        For a store private to one process that is shutting down. On a shared store this empties the queue of every
        replica, so the runner calls it only when ``shared`` is false.
        """
        ...


class InMemoryJobStore:
    """Jobs in this process (local development and tests). Returns the live job objects, not copies."""

    shared = False

    def __init__(self, clock: Clock, *, max_retained: int = 500) -> None:
        self._clock = clock
        self._max_retained = max_retained
        self._jobs: dict[str, InvestigationJob] = {}
        self._principals: dict[str, Principal] = {}
        self._claims: dict[str, tuple[str, int, datetime]] = {}  # job id -> (worker, claim, expires)
        self._claim_seq: dict[str, int] = {}
        self._attempts: dict[str, int] = {}
        self._active: dict[str, InvestigationJob] = {}
        self._latest: dict[str, str] = {}
        self._finished: list[str] = []
        self._failures: dict[str, int] = {}

    async def enqueue(self, job: InvestigationJob, principal: Principal) -> tuple[InvestigationJob, bool]:
        if (existing := self._active.get(job.incident_id)) is not None:
            return existing, False
        self._jobs[job.job_id] = job
        self._principals[job.job_id] = principal
        self._active[job.incident_id] = job
        self._latest[job.incident_id] = job.job_id
        return job, True

    async def get(self, job_id: str) -> InvestigationJob | None:
        return self._jobs.get(job_id)

    async def latest_for(self, incident_id: str) -> InvestigationJob | None:
        job_id = self._latest.get(incident_id)
        return self._jobs.get(job_id) if job_id else None

    async def active_incident_ids(self) -> set[str]:
        return set(self._active)

    async def is_active(self, incident_id: str) -> bool:
        return incident_id in self._active

    async def count_failed_attempts(self, incident_id: str) -> int:
        return self._failures.get(incident_id, 0)

    async def claim_next(self, worker_id: str, claim_ttl: timedelta, started_at: datetime) -> ClaimedJob | None:
        for job in self._jobs.values():  # insertion order is creation order
            if job.status is InvestigationJobStatus.QUEUED:
                claim = self._claim_seq.get(job.job_id, 0) + 1
                self._claim_seq[job.job_id] = claim
                self._claims[job.job_id] = (worker_id, claim, self._clock.now() + claim_ttl)
                job.status = InvestigationJobStatus.RUNNING
                job.started_at = started_at
                recovered = self._attempts.get(job.job_id, 0) > 0
                return ClaimedJob(job, self._principals[job.job_id], claim, recovered)
        return None

    def _owns(self, job_id: str, worker_id: str, claim: int) -> bool:
        held = self._claims.get(job_id)
        job = self._jobs.get(job_id)
        return (
            held is not None
            and held[0] == worker_id
            and held[1] == claim
            and job is not None
            and job.status is InvestigationJobStatus.RUNNING
        )

    async def heartbeat(self, job_id: str, worker_id: str, claim: int, claim_ttl: timedelta) -> bool:
        if not self._owns(job_id, worker_id, claim):
            return False
        self._claims[job_id] = (worker_id, claim, self._clock.now() + claim_ttl)
        return True

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
        if not self._owns(job_id, worker_id, claim):
            return False
        job = self._jobs[job_id]
        job.status, job.finished_at, job.error, job.result = status, finished_at, error, result
        del self._claims[job_id]
        self._deactivate(job)
        if status is InvestigationJobStatus.SUCCEEDED:
            self._failures.pop(job.incident_id, None)
        elif counts_attempt:
            self._failures[job.incident_id] = self._failures.get(job.incident_id, 0) + 1
        return True

    async def release(self, job_id: str, worker_id: str, claim: int) -> bool:
        if not self._owns(job_id, worker_id, claim):
            return False
        job = self._jobs[job_id]
        job.status, job.started_at = InvestigationJobStatus.QUEUED, None
        del self._claims[job_id]
        return True

    async def release_claims(self, worker_id: str) -> int:
        released = 0
        for job_id, (worker, _, _) in list(self._claims.items()):
            job = self._jobs[job_id]
            if worker == worker_id and job.status is InvestigationJobStatus.RUNNING:
                job.status, job.started_at = InvestigationJobStatus.QUEUED, None
                del self._claims[job_id]
                released += 1
        return released

    async def reclaim_expired(
        self, *, max_attempts: int, finished_at: datetime, exclude: Collection[str] = ()
    ) -> list[InvestigationJob]:
        failed: list[InvestigationJob] = []
        now = self._clock.now()
        for job_id, (_, _, expires) in list(self._claims.items()):
            job = self._jobs[job_id]
            if job.status is not InvestigationJobStatus.RUNNING or expires >= now or job_id in exclude:
                continue
            del self._claims[job_id]
            attempts = self._attempts[job_id] = self._attempts.get(job_id, 0) + 1
            if attempts >= max_attempts:
                job.status, job.finished_at = InvestigationJobStatus.FAILED, finished_at
                job.error = f"Abandoned: {attempts} workers stopped answering while running this investigation"
                self._deactivate(job)
                self._failures[job.incident_id] = self._failures.get(job.incident_id, 0) + 1
                failed.append(job)
            else:
                job.status, job.started_at = InvestigationJobStatus.QUEUED, None
        return failed

    async def cancel_queued(self, *, error: str, finished_at: datetime) -> list[InvestigationJob]:
        cancelled: list[InvestigationJob] = []
        for job in list(self._jobs.values()):
            if job.status is InvestigationJobStatus.QUEUED:
                job.status, job.error, job.finished_at = InvestigationJobStatus.FAILED, error, finished_at
                self._deactivate(job)
                cancelled.append(job)
        return cancelled

    def _deactivate(self, job: InvestigationJob) -> None:
        if self._active.get(job.incident_id) is job:
            del self._active[job.incident_id]
        self._finished.append(job.job_id)
        while len(self._finished) > self._max_retained:
            old_id = self._finished.pop(0)
            old = self._jobs.pop(old_id, None)
            self._principals.pop(old_id, None)
            self._claim_seq.pop(old_id, None)
            self._attempts.pop(old_id, None)
            if old is not None and self._latest.get(old.incident_id) == old_id:
                del self._latest[old.incident_id]
