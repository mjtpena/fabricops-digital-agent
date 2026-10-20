"""Investigations as background jobs.

``POST /incidents/{id}/investigate`` used to run the whole investigation inside the HTTP request, so a slow LLM
investigation could outlive the ingress timeout. It now *submits* a job and returns at once; the scheduler's
auto-investigation submits through exactly the same runner. The investigation itself is unchanged: the runner calls
:meth:`~orqentis.incidents.investigation.InvestigationService.investigate`.

* **Idempotent per incident.** While a job for an incident is queued or running, submitting again returns that job.
* **Bounded.** At most ``max_concurrency`` investigations run in this process at once (the rest wait in the queue) and
  each one has a timeout that starts when it takes its slot; an investigation that is cancelled or times out puts its
  incident back to ``DETECTED``.
* **Where jobs live is the** :class:`~orqentis.runtime.job_store.JobStore`. The default keeps them in this process
  (local development: the last ``max_retained`` finished jobs, no polling). The PostgreSQL store makes them durable
  and shared: ``POST /incidents/{id}/investigate`` can land on any replica, the job is visible to every replica, and
  *every* replica runs a worker loop that claims queued jobs (``FOR UPDATE SKIP LOCKED``), keeps its claims alive with a
  heartbeat, and re-claims the jobs of a replica that died. A graceful shutdown hands running jobs back to the queue.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from orqentis.config.clock import Clock
from orqentis.domain.common import new_id
from orqentis.domain.incidents import IncidentStatus
from orqentis.domain.principals import SYSTEM_PRINCIPAL, Principal
from orqentis.incidents.investigation import INVESTIGABLE, InvestigationNotPossibleError, InvestigationService
from orqentis.incidents.service import IncidentService
from orqentis.observability.audit import AuditTrail
from orqentis.observability.logging import get_logger
from orqentis.observability.redaction import redact_text
from orqentis.runtime.fencing import ClaimFence, FencingError, claim_fenced, fenced
from orqentis.runtime.job_store import (
    ClaimedJob,
    InMemoryJobStore,
    InvestigationJob,
    InvestigationJobNotFoundError,
    InvestigationJobStatus,
    InvestigationOutcome,
    InvestigationTrigger,
    JobStore,
)
from orqentis.runtime.lease import new_holder_id
from orqentis.storage.incidents import IncidentQuery
from orqentis.topology.catalog import EstateCatalog

__all__ = [
    "AutoInvestigationTask",
    "InvestigationJob",
    "InvestigationJobNotFoundError",
    "InvestigationJobRunner",
    "InvestigationJobStatus",
    "InvestigationOutcome",
    "InvestigationTrigger",
    "Submission",
]

_log = get_logger(__name__)

_MAX_ERROR_TEXT = 500
_SHUTDOWN_REASON = "Cancelled: the service is shutting down"


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
        store: JobStore | None = None,
        max_concurrency: int = 2,
        timeout: timedelta = timedelta(minutes=10),
        max_retained: int = 500,
        worker_id: str | None = None,
        poll_interval: timedelta = timedelta(seconds=2),
        claim_ttl: timedelta = timedelta(seconds=120),
        max_claims: int = 3,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be >= 1")
        if claim_ttl < poll_interval * 3:
            raise ValueError("claim_ttl must be at least three times poll_interval")
        self._service = service
        self._incidents = incidents
        self._audit = audit
        self._clock = clock
        self._timeout = timeout
        self._max_concurrency = max_concurrency
        self._store: JobStore = store if store is not None else InMemoryJobStore(clock, max_retained=max_retained)
        self._worker_id = worker_id or new_holder_id()
        self._poll_interval = poll_interval
        self._claim_ttl = claim_ttl
        self._max_claims = max_claims
        self._running: dict[str, asyncio.Task[None]] = {}  # job id -> its task, in this process
        self._lost: set[str] = set()  # jobs whose claim another worker took over while we were running them
        self._unentered: dict[str, ClaimedJob] = {}  # claimed, task created, task body not started yet
        self._tasks: set[asyncio.Task[None]] = set()
        self._pumping = False
        self._pump_again = False
        self._poller: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self._closed = False

    # --------------------------------------------------------------------------------------------- queries
    async def get(self, job_id: str) -> InvestigationJob | None:
        return await self._store.get(job_id)

    async def latest_for(self, incident_id: str) -> InvestigationJob | None:
        return await self._store.latest_for(incident_id)

    async def is_active(self, incident_id: str) -> bool:
        """Whether a job for the incident is queued or running anywhere (any replica)."""
        return await self._store.is_active(incident_id)

    async def active_incident_ids(self) -> set[str]:
        return await self._store.active_incident_ids()

    async def failed_attempts(self, incident_id: str) -> int:
        """Investigations of this incident that actually failed since it last succeeded.

        An investigation that was cancelled because a service is shutting down is not a failed attempt.
        """
        return await self._store.count_failed_attempts(incident_id)

    # -------------------------------------------------------------------------------------------- commands
    async def submit(self, incident_id: str, *, requested_by: Principal, trigger: InvestigationTrigger) -> Submission:
        """Queue an investigation, or return the one already queued or running for this incident.

        Raises ``IncidentNotFoundError`` for an unknown incident and :class:`InvestigationNotPossibleError` when the
        incident is not in an investigable state or has no workflow, exactly as the synchronous call used to.
        """
        if self._closed:
            raise InvestigationNotPossibleError("The investigation runner is shutting down")
        if (latest := await self._store.latest_for(incident_id)) is not None and latest.status.is_active:
            return Submission(latest, created=False)
        incident = await self._incidents.get(incident_id)
        if incident.status not in INVESTIGABLE:
            raise InvestigationNotPossibleError(
                f"Incident {incident_id} is {incident.status.value}; cannot investigate"
            )
        if self._service.workflow_for(incident) is None:
            raise InvestigationNotPossibleError(
                f"No investigation workflow for {incident.primary_item_type} incidents yet"
            )
        # The store decides atomically: of two submissions racing (even on different replicas) exactly one creates the
        # job and the other is handed that job.
        job, created = await self._store.enqueue(
            InvestigationJob(
                job_id=new_id("INV"),
                incident_id=incident_id,
                status=InvestigationJobStatus.QUEUED,
                trigger=trigger,
                requested_by=requested_by.id,
                created_at=self._clock.now(),
            ),
            requested_by,
        )
        if created:
            _log.info(
                "investigation queued",
                extra={"job_id": job.job_id, "incident_id": incident_id, "trigger": trigger.value},
            )
            self._request_pump()
        return Submission(job, created=created)

    async def start(self) -> None:
        """Start the worker loop that claims queued jobs. Only a shared store needs one (others may enqueue)."""
        if not self._store.shared or self._poller is not None or self._closed:
            return
        self._poller = asyncio.create_task(self._poll_loop(), name="investigation-worker")

    async def wait_idle(self) -> None:
        """Wait for every investigation started by this process to finish (tests and shutdown)."""
        while True:
            # Yield first: a task that has just finished only leaves ``_tasks`` (and starts the next queued job) in its
            # done callback, which runs on the next loop iteration. gather() of finished tasks does not yield at all.
            await asyncio.sleep(0)
            pending = [task for task in self._tasks if not task.done()]
            if not pending:
                return
            await asyncio.gather(*pending, return_exceptions=True)

    async def shutdown(self) -> None:
        """Stop accepting work and cancel what is running.

        With a private store the queued and running jobs end as ``FAILED`` ("shutting down", not counted as an
        attempt) and their incidents go back to ``DETECTED``. With a shared store, running jobs are handed back to the
        queue for another replica, and queued jobs are left where they are.
        """
        self._closed = True
        poller, self._poller = self._poller, None
        if poller is not None:
            poller.cancel()
            await asyncio.gather(poller, return_exceptions=True)
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        # A task cancelled before its first step never ran any of its own code, so it could not hand its job back.
        for claimed in list(self._unentered.values()):
            if self._store.shared:
                await self._store.release(claimed.job.job_id, self._worker_id, claimed.claim)
            else:
                await self._finish(claimed, InvestigationJobStatus.FAILED, error=_SHUTDOWN_REASON, counts=False)
        self._unentered.clear()
        if self._store.shared:
            # A claim can commit just as the worker loop is cancelled: it then belongs to no task. Give it back now
            # instead of leaving it RUNNING until its claim expires (which would also cost the job an attempt).
            if (orphaned := await self._store.release_claims(self._worker_id)) > 0:
                _log.info("released claims that no task had started", extra={"jobs": orphaned})
        else:
            # Jobs that never got a slot have no task to mark them.
            for job in await self._store.cancel_queued(error=_SHUTDOWN_REASON, finished_at=self._clock.now()):
                _log.info("queued investigation cancelled by shutdown", extra={"job_id": job.job_id})
                await self._audit_failure(job)

    async def reset_stale(self, incident_id: str, *, reason: str) -> bool:
        """Put an incident stuck in ``INVESTIGATING`` back to ``DETECTED`` (no live job is working on it)."""
        if await self._store.is_active(incident_id):
            return False
        return await self._reset(incident_id, reason=reason)

    # ----------------------------------------------------------------------------------------------- workers
    def _request_pump(self) -> None:
        if self._closed:
            return
        self._wake.set()
        if self._store.shared and self._poller is not None:
            return  # the worker loop claims it
        if self._pumping:
            self._pump_again = True
            return
        self._pumping = True
        task = asyncio.create_task(self._pump(), name="investigation-pump")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _poll_loop(self) -> None:
        while True:
            try:
                await self._pump_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                _log.warning("investigation worker poll failed", exc_info=True)
            self._wake.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=self._poll_interval.total_seconds())

    async def _pump(self) -> None:
        try:
            while True:
                self._pump_again = False
                try:
                    await self._pump_once()
                except Exception:
                    _log.warning("could not claim queued investigations", exc_info=True)
                if not self._pump_again:
                    break
        finally:
            self._pumping = False

    async def _pump_once(self) -> None:
        """Claim queued jobs while there is a free slot. Runs outside any scheduled job's fence."""
        with fenced(None):
            if self._store.shared:
                await self._reclaim()
            while not self._closed and len(self._running) < self._max_concurrency:
                claimed = await self._store.claim_next(self._worker_id, self._claim_ttl, self._clock.now())
                if claimed is None:
                    return
                self._start(claimed)

    def _start(self, claimed: ClaimedJob) -> None:
        job_id = claimed.job.job_id
        with fenced(None):  # the task must not inherit the fence of the scheduled job that happened to enqueue it
            task = asyncio.create_task(self._run(claimed), name=f"investigation:{job_id}")
        self._running[job_id] = task
        self._unentered[job_id] = claimed
        self._tasks.add(task)

        def on_done(finished: asyncio.Task[None]) -> None:
            self._finished_task(finished, job_id)

        task.add_done_callback(on_done)

    def _finished_task(self, task: asyncio.Task[None], job_id: str) -> None:
        self._running.pop(job_id, None)
        self._lost.discard(job_id)
        self._tasks.discard(task)
        self._request_pump()  # a slot is free: take the next queued job

    async def _reclaim(self) -> None:
        # Never take back a job this process is still running: after a database outage longer than the claim TTL its
        # claim looks expired, but its task is alive and will heartbeat again (or find the claim lost, if another
        # replica took it).
        expired = await self._store.reclaim_expired(
            max_attempts=self._max_claims, finished_at=self._clock.now(), exclude=list(self._running)
        )
        for job in expired:
            _log.warning("investigation abandoned by its workers", extra={"job_id": job.job_id})
            await self._reset(job.incident_id, reason=job.error or "abandoned")
            await self._audit_failure(job)

    # ----------------------------------------------------------------------------------------------- internals
    async def _run(self, claimed: ClaimedJob) -> None:
        job, actor = claimed.job, claimed.principal
        self._unentered.pop(job.job_id, None)
        run_task = asyncio.current_task()
        heartbeat = (
            asyncio.create_task(self._heartbeat(claimed, run_task), name=f"heartbeat:{job.job_id}")
            if self._store.shared and run_task is not None
            else None
        )
        budget: asyncio.Timeout | None = None
        try:
            if claimed.recovered:
                # The worker that had this job died; its incident is still INVESTIGATING and cannot be investigated.
                await self._reset(job.incident_id, reason="a previous worker stopped while investigating it")
            # The timeout bounds the investigation, not the wait for a slot: starting the clock any earlier would let
            # jobs queued behind busy slots expire without ever having run (an incident storm).
            budget = asyncio.timeout(self._timeout.total_seconds())
            fence = ClaimFence(job.job_id, self._worker_id, claimed.claim) if self._store.shared else None
            with claim_fenced(fence):  # database writes of the investigation are refused once the claim is not ours
                async with budget:
                    incident, trace = await self._service.investigate(job.incident_id, actor=actor)
            outcome = InvestigationOutcome(
                incident_status=incident.status,
                trace_id=trace.trace_id,
                findings=len(incident.findings),
                evidence=len(incident.evidence),
                tool_calls=len(trace.tool_calls),
                approval_ids=list(incident.approval_ids),
                confidence_level=incident.confidence.level.value if incident.confidence else None,
                confidence_score=incident.confidence.score if incident.confidence else None,
            )
            await self._finish(claimed, InvestigationJobStatus.SUCCEEDED, result=outcome)
        except asyncio.CancelledError:
            await self._on_cancelled(claimed)
            raise
        except FencingError:
            # The claim was taken over while the investigation was writing: the new holder owns the job and incident.
            _log.warning("investigation write refused: the claim was lost", extra={"job_id": job.job_id})
            self._lost.add(job.job_id)
        except TimeoutError as exc:
            if budget is not None and budget.expired():
                await self._fail(claimed, f"Timed out after {self._timeout.total_seconds():g}s", reset=True)
            else:
                await self._fail(
                    claimed, f"{type(exc).__name__}: {redact_text(str(exc))}"[:_MAX_ERROR_TEXT], reset=True
                )
        except Exception as exc:
            # InvestigationService has already returned the incident to DETECTED and audited the abort.
            await self._fail(claimed, f"{type(exc).__name__}: {redact_text(str(exc))}"[:_MAX_ERROR_TEXT], reset=False)
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)

    async def _heartbeat(self, claimed: ClaimedJob, run_task: asyncio.Task[Any] | None) -> None:
        """Keep the claim alive; stop the investigation as soon as another replica could have taken the job over.

        A failing heartbeat is retried, but only while the claim cannot yet have expired: once the last heartbeat that
        succeeded is older than the claim lifetime minus one interval, any replica may re-queue and start the job, so
        this worker stops its own run (it would otherwise investigate the same incident a second time at once). What it
        might still write in the meantime is refused by the claim fence.
        """
        job = claimed.job
        interval = self._claim_ttl.total_seconds() / 3
        loop = asyncio.get_running_loop()
        last_renewed = loop.time()
        while True:
            asleep_since = loop.time()
            await asyncio.sleep(interval)
            if (late := loop.time() - asleep_since - interval) > interval:
                # The loop was blocked (a CPU-bound step, a paused container). If this lasts a whole claim lifetime,
                # another replica takes the job over; the claim fence then refuses what this run still tries to write.
                _log.warning(
                    "event loop stalled; the claim heartbeat ran late",
                    extra={"job_id": job.job_id, "late_seconds": round(late, 2)},
                )
            try:
                alive = await asyncio.wait_for(
                    self._store.heartbeat(job.job_id, self._worker_id, claimed.claim, self._claim_ttl), interval
                )
            except Exception:
                _log.warning("investigation heartbeat failed", extra={"job_id": job.job_id}, exc_info=True)
                if loop.time() - last_renewed > self._claim_ttl.total_seconds() - interval:
                    _log.error(
                        "investigation claim could not be renewed for as long as its lifetime allows; stopping",
                        extra={"job_id": job.job_id},
                    )
                    self._abandon(job.job_id, run_task)
                    return
                continue
            if not alive:
                _log.warning("investigation claim lost; stopping", extra={"job_id": job.job_id})
                self._abandon(job.job_id, run_task)
                return
            last_renewed = loop.time()

    def _abandon(self, job_id: str, run_task: asyncio.Task[Any] | None) -> None:
        """The job is, or may be, someone else's now: stop running it and touch nothing of it afterwards."""
        self._lost.add(job_id)
        if run_task is not None:
            run_task.cancel()

    async def _on_cancelled(self, claimed: ClaimedJob) -> None:
        job = claimed.job
        if job.job_id in self._lost:
            return  # another worker owns the job now; it is theirs to finish or fail
        # A shutdown says nothing about the incident, so it does not use up a retry.
        await self._reset(job.incident_id, reason=_SHUTDOWN_REASON)
        if self._store.shared:
            await self._store.release(job.job_id, self._worker_id, claimed.claim)
        else:
            await self._fail(claimed, _SHUTDOWN_REASON, reset=False, counts=False)

    async def _finish(
        self,
        claimed: ClaimedJob,
        status: InvestigationJobStatus,
        *,
        error: str | None = None,
        result: InvestigationOutcome | None = None,
        counts: bool = True,
    ) -> bool:
        stored = await self._store.finish(
            claimed.job.job_id,
            self._worker_id,
            claimed.claim,
            status=status,
            finished_at=self._clock.now(),
            error=error,
            result=result,
            counts_attempt=counts,
        )
        if not stored:
            _log.warning("investigation result discarded: the claim was lost", extra={"job_id": claimed.job.job_id})
        return stored

    async def _fail(self, claimed: ClaimedJob, error: str, *, reset: bool, counts: bool = True) -> None:
        job = claimed.job
        _log.warning(
            "investigation job failed", extra={"job_id": job.job_id, "incident_id": job.incident_id, "error": error}
        )
        if reset:
            await self._reset(job.incident_id, reason=error)
        if await self._finish(claimed, InvestigationJobStatus.FAILED, error=error, counts=counts):
            await self._audit_failure(job.model_copy(update={"error": error}))

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


class AutoInvestigationTask:
    """Scheduled job: investigate incidents that are ``DETECTED`` and not already being investigated."""

    def __init__(
        self,
        incidents: IncidentService,
        runner: InvestigationJobRunner,
        clock: Clock,
        *,
        catalog: EstateCatalog | None = None,
        batch_size: int = 5,
        max_attempts: int = 3,
        orphan_after: timedelta = timedelta(minutes=15),
    ) -> None:
        self._incidents = incidents
        self._runner = runner
        self._clock = clock
        self._catalog = catalog
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
            if await self._runner.is_active(incident.incident_id):
                continue
            if self._catalog is not None and self._catalog.node(incident.primary_item_id) is None:
                # Its item left the estate (the catalog refresh noted that on the incident): nothing to investigate.
                _log.debug("auto-investigation skipped: item gone", extra={"incident_id": incident.incident_id})
                continue
            if await self._runner.failed_attempts(incident.incident_id) >= self._max_attempts:
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
        """An incident left in ``INVESTIGATING`` by a process that died would otherwise never be picked up again.

        "No live investigation" is judged by the job store, so with a shared store a replica never mistakes another
        replica's running investigation for an orphan.
        """
        stuck = await self._incidents.repository.list(IncidentQuery(statuses=[IncidentStatus.INVESTIGATING], limit=200))
        cutoff = self._clock.now() - self._orphan_after
        for incident in stuck:
            if incident.updated_at <= cutoff and await self._runner.reset_stale(
                incident.incident_id, reason="no live investigation found for this incident"
            ):
                _log.warning("orphaned investigation reset", extra={"incident_id": incident.incident_id})
