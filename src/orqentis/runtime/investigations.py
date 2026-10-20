"""Investigations as background jobs.

``POST /incidents/{id}/investigate`` used to run the whole investigation inside the HTTP request, so a slow LLM
investigation could outlive the ingress timeout. It now *submits* a job and returns at once; the scheduler's
auto-investigation submits through exactly the same runner. The investigation itself is unchanged: the runner calls
:meth:`~orqentis.incidents.investigation.InvestigationService.investigate`.

* **Idempotent per incident.** While a job for an incident is queued or running, submitting again returns that job.
* **Bounded.** At most ``max_concurrency`` investigations run at once (the rest are queued) and each one has a
  timeout; an investigation that is cancelled or times out puts its incident back to ``DETECTED``.
* **In memory.** Jobs live in this process (the last ``max_retained`` finished jobs are kept for status queries).
  Durable job storage arrives with the PostgreSQL system of record.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from orqentis.config.clock import Clock
from orqentis.domain.common import new_id
from orqentis.domain.incidents import IncidentStatus
from orqentis.domain.principals import SYSTEM_PRINCIPAL, Principal
from orqentis.incidents.investigation import INVESTIGABLE, InvestigationNotPossibleError, InvestigationService
from orqentis.incidents.service import IncidentService
from orqentis.observability.audit import AuditTrail
from orqentis.observability.logging import get_logger
from orqentis.observability.redaction import redact_text
from orqentis.storage.incidents import IncidentQuery

_log = get_logger(__name__)

_MAX_ERROR_TEXT = 500


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
class Submission:
    job: InvestigationJob
    # False when an investigation of the incident was already queued or running and that job was returned.
    created: bool


class InvestigationJobRunner:
    def __init__(
        self,
        service: InvestigationService,
        incidents: IncidentService,
        audit: AuditTrail,
        clock: Clock,
        *,
        max_concurrency: int = 2,
        timeout: timedelta = timedelta(minutes=10),
        max_retained: int = 500,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be >= 1")
        self._service = service
        self._incidents = incidents
        self._audit = audit
        self._clock = clock
        self._timeout = timeout
        self._max_retained = max_retained
        self._slots = asyncio.Semaphore(max_concurrency)
        self._jobs: dict[str, InvestigationJob] = {}
        self._active: dict[str, InvestigationJob] = {}  # incident id -> its queued/running job
        self._latest: dict[str, str] = {}  # incident id -> most recent job id
        self._finished: deque[str] = deque()
        self._tasks: set[asyncio.Task[None]] = set()
        self._failures: dict[str, int] = {}
        self._closed = False

    # --------------------------------------------------------------------------------------------- queries
    def get(self, job_id: str) -> InvestigationJob | None:
        return self._jobs.get(job_id)

    def latest_for(self, incident_id: str) -> InvestigationJob | None:
        job_id = self._latest.get(incident_id)
        return self._jobs.get(job_id) if job_id else None

    def is_active(self, incident_id: str) -> bool:
        return incident_id in self._active

    def active_incident_ids(self) -> set[str]:
        return set(self._active)

    def failed_attempts(self, incident_id: str) -> int:
        """Investigations of this incident that failed since it last succeeded (this process only)."""
        return self._failures.get(incident_id, 0)

    # -------------------------------------------------------------------------------------------- commands
    async def submit(self, incident_id: str, *, requested_by: Principal, trigger: InvestigationTrigger) -> Submission:
        """Queue an investigation, or return the one already queued or running for this incident.

        Raises ``IncidentNotFoundError`` for an unknown incident and :class:`InvestigationNotPossibleError` when the
        incident is not in an investigable state or has no workflow, exactly as the synchronous call used to.
        """
        if self._closed:
            raise InvestigationNotPossibleError("The investigation runner is shutting down")
        if (existing := self._active.get(incident_id)) is not None:
            return Submission(existing, created=False)
        incident = await self._incidents.get(incident_id)
        if incident.status not in INVESTIGABLE:
            raise InvestigationNotPossibleError(
                f"Incident {incident_id} is {incident.status.value}; cannot investigate"
            )
        if self._service.workflow_for(incident) is None:
            raise InvestigationNotPossibleError(
                f"No investigation workflow for {incident.primary_item_type} incidents yet"
            )
        # The lookups above awaited: another request may have queued a job meanwhile. There is no await from here
        # to the registration below, so exactly one job per incident can be created.
        if (existing := self._active.get(incident_id)) is not None:
            return Submission(existing, created=False)
        job = InvestigationJob(
            job_id=new_id("INV"),
            incident_id=incident_id,
            status=InvestigationJobStatus.QUEUED,
            trigger=trigger,
            requested_by=requested_by.id,
            created_at=self._clock.now(),
        )
        self._jobs[job.job_id] = job
        self._active[incident_id] = job
        self._latest[incident_id] = job.job_id
        task = asyncio.create_task(self._run(job, requested_by), name=f"investigation:{job.job_id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        _log.info(
            "investigation queued",
            extra={"job_id": job.job_id, "incident_id": incident_id, "trigger": trigger.value},
        )
        return Submission(job, created=True)

    async def wait_idle(self) -> None:
        """Wait for every queued and running investigation to finish (tests and shutdown)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def shutdown(self) -> None:
        """Stop accepting work and cancel what is queued or running; their incidents go back to ``DETECTED``."""
        self._closed = True
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def reset_stale(self, incident_id: str, *, reason: str) -> bool:
        """Put an incident stuck in ``INVESTIGATING`` back to ``DETECTED`` (no live job of ours is working on it)."""
        if incident_id in self._active:
            return False
        return await self._reset(incident_id, reason=reason)

    # ----------------------------------------------------------------------------------------------- internals
    async def _run(self, job: InvestigationJob, actor: Principal) -> None:
        budget = asyncio.timeout(self._timeout.total_seconds())
        try:
            async with self._slots:
                job.status = InvestigationJobStatus.RUNNING
                job.started_at = self._clock.now()
                async with budget:
                    incident, trace = await self._service.investigate(job.incident_id, actor=actor)
                job.result = InvestigationOutcome(
                    incident_status=incident.status,
                    trace_id=trace.trace_id,
                    findings=len(incident.findings),
                    evidence=len(incident.evidence),
                    tool_calls=len(trace.tool_calls),
                    approval_ids=list(incident.approval_ids),
                    confidence_level=incident.confidence.level.value if incident.confidence else None,
                    confidence_score=incident.confidence.score if incident.confidence else None,
                )
                job.status = InvestigationJobStatus.SUCCEEDED
                self._failures.pop(job.incident_id, None)
        except asyncio.CancelledError:
            await self._fail(job, "Cancelled: the service is shutting down", reset=True)
            raise
        except TimeoutError as exc:
            if budget.expired():
                await self._fail(job, f"Timed out after {self._timeout.total_seconds():.0f}s", reset=True)
            else:
                await self._fail(job, f"{type(exc).__name__}: {redact_text(str(exc))}"[:_MAX_ERROR_TEXT], reset=True)
        except Exception as exc:
            # InvestigationService has already returned the incident to DETECTED and audited the abort.
            await self._fail(job, f"{type(exc).__name__}: {redact_text(str(exc))}"[:_MAX_ERROR_TEXT], reset=False)
        finally:
            job.finished_at = self._clock.now()
            if self._active.get(job.incident_id) is job:
                del self._active[job.incident_id]
            self._remember_finished(job)

    async def _fail(self, job: InvestigationJob, error: str, *, reset: bool) -> None:
        job.status = InvestigationJobStatus.FAILED
        job.error = error
        self._failures[job.incident_id] = self._failures.get(job.incident_id, 0) + 1
        _log.warning(
            "investigation job failed", extra={"job_id": job.job_id, "incident_id": job.incident_id, "error": error}
        )
        if reset:
            await self._reset(job.incident_id, reason=error)
        await self._audit_failure(job)

    async def _reset(self, incident_id: str, *, reason: str) -> bool:
        try:
            current = await self._incidents.repository.get(incident_id)
            if current is None or current.status is not IncidentStatus.INVESTIGATING:
                return False
            await self._incidents.transition(
                current,
                IncidentStatus.DETECTED,
                actor=SYSTEM_PRINCIPAL.id,
                reason=f"Investigation did not complete: {reason}"[:_MAX_ERROR_TEXT],
            )
            return True
        except Exception:
            _log.exception("could not reset incident after failed investigation", extra={"incident_id": incident_id})
            return False

    async def _audit_failure(self, job: InvestigationJob) -> None:
        details: dict[str, Any] = {"job_id": job.job_id, "trigger": job.trigger.value, "error": job.error}
        try:
            await self._audit.record(
                actor=SYSTEM_PRINCIPAL.id,
                action="investigation.job",
                outcome="FAILED",
                incident_id=job.incident_id,
                details=details,
            )
        except Exception:
            _log.warning("could not audit failed investigation job", exc_info=True)

    def _remember_finished(self, job: InvestigationJob) -> None:
        self._finished.append(job.job_id)
        while len(self._finished) > self._max_retained:
            old = self._jobs.pop(self._finished.popleft(), None)
            if old is not None and self._latest.get(old.incident_id) == old.job_id:
                del self._latest[old.incident_id]


class AutoInvestigationTask:
    """Scheduled job: investigate incidents that are ``DETECTED`` and not already being investigated."""

    def __init__(
        self,
        incidents: IncidentService,
        runner: InvestigationJobRunner,
        clock: Clock,
        *,
        batch_size: int = 5,
        max_attempts: int = 3,
        orphan_after: timedelta = timedelta(minutes=15),
    ) -> None:
        self._incidents = incidents
        self._runner = runner
        self._clock = clock
        self._batch_size = batch_size
        self._max_attempts = max_attempts
        self._orphan_after = orphan_after

    async def run(self) -> None:
        await self._recover_orphans()
        detected = await self._incidents.repository.list(IncidentQuery(statuses=[IncidentStatus.DETECTED], limit=200))
        # Most severe first, then oldest first.
        detected.sort(key=lambda i: (-i.severity.rank, i.detected_at))
        submitted = 0
        for incident in detected:
            if submitted >= self._batch_size:
                break
            if self._runner.is_active(incident.incident_id):
                continue
            if self._runner.failed_attempts(incident.incident_id) >= self._max_attempts:
                continue  # give up quietly; a human can still investigate it via the API
            try:
                await self._runner.submit(
                    incident.incident_id, requested_by=SYSTEM_PRINCIPAL, trigger=InvestigationTrigger.SCHEDULER
                )
            except InvestigationNotPossibleError as exc:
                _log.debug(
                    "auto-investigation skipped", extra={"incident_id": incident.incident_id, "reason": str(exc)}
                )
                continue
            submitted += 1
        if submitted:
            _log.info("auto-investigation submitted", extra={"jobs": submitted})

    async def _recover_orphans(self) -> None:
        """An incident left in ``INVESTIGATING`` by a process that died would otherwise never be picked up again."""
        stuck = await self._incidents.repository.list(IncidentQuery(statuses=[IncidentStatus.INVESTIGATING], limit=200))
        cutoff = self._clock.now() - self._orphan_after
        for incident in stuck:
            if incident.updated_at <= cutoff and await self._runner.reset_stale(
                incident.incident_id, reason="no live investigation found for this incident"
            ):
                _log.warning("orphaned investigation reset", extra={"incident_id": incident.incident_id})
