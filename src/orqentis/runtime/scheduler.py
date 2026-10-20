"""In-process job scheduler gated by a leader lease.

* Only the lease holder runs jobs. Leadership is re-checked (and renewed) on every tick; when it is lost, running
  jobs are cancelled and nothing new starts until the lease is acquired again.
* A job never overlaps with itself: a run that is still going when its next slot arrives makes that slot be
  skipped (and counted), it is never stacked or queued. Slots missed while the process was busy collapse into one.
* A failing job is logged (and audited when its health changes) and never stops the scheduler or the other jobs.
* Time is read from the injected :class:`~orqentis.config.clock.Clock`, so tests drive the scheduler by advancing a
  fake clock and calling :meth:`Scheduler.tick`; :meth:`Scheduler.start` merely calls ``tick`` every few seconds.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from pydantic import AwareDatetime, BaseModel, ConfigDict

from orqentis.config.clock import Clock
from orqentis.domain.principals import SYSTEM_PRINCIPAL
from orqentis.observability.audit import AuditTrail
from orqentis.observability.logging import get_logger
from orqentis.observability.redaction import redact_text
from orqentis.runtime.lease import LeaderLease, Lease

_log = get_logger(__name__)

JobFunction = Callable[[], Awaitable[None]]
Sleep = Callable[[float], Awaitable[None]]

DEFAULT_LEASE_NAME = "orqentis-scheduler"
_MAX_ERROR_TEXT = 300


@dataclass(frozen=True)
class JobSpec:
    """A recurring job. ``run`` must be safe to repeat (collection and detection are idempotent)."""

    name: str
    interval: timedelta
    run: JobFunction
    enabled: bool = True
    # Real elapsed time after which a run is cancelled; ``None`` uses the scheduler default.
    timeout: timedelta | None = None
    # True: first run on the first tick as leader. False: first run one interval later.
    run_immediately: bool = True

    def __post_init__(self) -> None:
        if self.interval <= timedelta(0):
            raise ValueError(f"job {self.name!r}: interval must be positive")


class JobStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    enabled: bool
    interval_seconds: float
    running: bool
    next_run_at: AwareDatetime | None
    run_count: int
    failure_count: int
    consecutive_failures: int
    skipped_overlaps: int
    last_started_at: AwareDatetime | None
    last_finished_at: AwareDatetime | None
    last_outcome: str | None
    last_error: str | None


class SchedulerStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    running: bool
    leader: bool
    holder_id: str
    fencing_token: int | None
    jobs: list[JobStatus]


@dataclass
class _JobState:
    spec: JobSpec
    next_run_at: datetime | None = None
    task: asyncio.Task[None] | None = None
    run_count: int = 0
    failure_count: int = 0
    consecutive_failures: int = 0
    skipped_overlaps: int = 0
    last_started_at: datetime | None = None
    last_finished_at: datetime | None = None
    last_outcome: str | None = None
    last_error: str | None = None
    # Whether the last *reported* health was a failure (audit is written on changes only, not on every retry).
    reported_failing: bool = field(default=False)

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    def advance_past(self, now: datetime) -> None:
        """Move ``next_run_at`` to the first slot after ``now`` (missed slots collapse into one run)."""
        assert self.next_run_at is not None
        missed = (now - self.next_run_at) // self.spec.interval + 1
        self.next_run_at += self.spec.interval * max(missed, 1)


class Scheduler:
    def __init__(
        self,
        jobs: Sequence[JobSpec],
        lease: LeaderLease,
        clock: Clock,
        *,
        lease_name: str = DEFAULT_LEASE_NAME,
        lease_ttl: timedelta = timedelta(seconds=60),
        tick_interval: timedelta = timedelta(seconds=5),
        default_job_timeout: timedelta | None = timedelta(minutes=30),
        audit: AuditTrail | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        names = [j.name for j in jobs]
        if len(set(names)) != len(names):
            raise ValueError("job names must be unique")
        self._states = {j.name: _JobState(j) for j in jobs}
        self._lease_port = lease
        self._clock = clock
        self._lease_name = lease_name
        self._lease_ttl = lease_ttl
        self._tick_interval = tick_interval
        self._default_timeout = default_job_timeout
        self._audit = audit
        self._sleep = sleep
        self._lease: Lease | None = None
        self._tick_lock = asyncio.Lock()
        self._loop_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------------------------------- lifecycle
    @property
    def is_running(self) -> bool:
        return self._loop_task is not None and not self._loop_task.done()

    @property
    def is_leader(self) -> bool:
        return self._lease is not None

    async def start(self) -> None:
        if self.is_running:
            return
        self._loop_task = asyncio.create_task(self._loop(), name="scheduler")
        _log.info("scheduler started", extra={"jobs": [n for n, s in self._states.items() if s.spec.enabled]})

    async def stop(self) -> None:
        """Stop ticking, cancel running jobs and give the lease up so a standby can take over at once."""
        loop_task, self._loop_task = self._loop_task, None
        if loop_task is not None:
            loop_task.cancel()
            await asyncio.gather(loop_task, return_exceptions=True)
        async with self._tick_lock:
            await self._cancel_running()
            lease, self._lease = self._lease, None
            if lease is not None:
                try:
                    await self._lease_port.release(lease)
                except Exception:
                    _log.warning("scheduler lease release failed", exc_info=True)
        _log.info("scheduler stopped")

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                _log.exception("scheduler tick failed")
            await self._sleep(self._tick_interval.total_seconds())

    # ------------------------------------------------------------------------------------------------ tick
    async def tick(self) -> list[str]:
        """Start every job that is due and not already running. Returns the names that were started."""
        async with self._tick_lock:
            if not await self._ensure_leadership():
                return []
            now = self._clock.now()
            started: list[str] = []
            for state in self._states.values():
                spec = state.spec
                if not spec.enabled:
                    continue
                if state.next_run_at is None:
                    state.next_run_at = now if spec.run_immediately else now + spec.interval
                if now < state.next_run_at:
                    continue
                if state.running:
                    state.skipped_overlaps += 1
                    state.advance_past(now)
                    _log.warning(
                        "scheduled job still running; slot skipped",
                        extra={"job": spec.name, "skipped_overlaps": state.skipped_overlaps},
                    )
                    continue
                state.advance_past(now)
                state.task = asyncio.create_task(self._run(state), name=f"job:{spec.name}")
                started.append(spec.name)
            return started

    async def wait_idle(self) -> None:
        """Wait until no job is running (tests and shutdown)."""
        tasks = [s.task for s in self._states.values() if s.task is not None and not s.task.done()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def status(self) -> SchedulerStatus:
        return SchedulerStatus(
            running=self.is_running,
            leader=self.is_leader,
            holder_id=self._lease_port.holder_id,
            fencing_token=self._lease.fencing_token if self._lease else None,
            jobs=[
                JobStatus(
                    name=s.spec.name,
                    enabled=s.spec.enabled,
                    interval_seconds=s.spec.interval.total_seconds(),
                    running=s.running,
                    next_run_at=s.next_run_at,
                    run_count=s.run_count,
                    failure_count=s.failure_count,
                    consecutive_failures=s.consecutive_failures,
                    skipped_overlaps=s.skipped_overlaps,
                    last_started_at=s.last_started_at,
                    last_finished_at=s.last_finished_at,
                    last_outcome=s.last_outcome,
                    last_error=s.last_error,
                )
                for s in self._states.values()
            ],
        )

    # -------------------------------------------------------------------------------------------- leadership
    async def _ensure_leadership(self) -> bool:
        now = self._clock.now()
        try:
            if self._lease is None:
                lease = await self._lease_port.acquire(self._lease_name, self._lease_ttl)
                if lease is None:
                    return False
                self._lease = lease
                await self._record_leadership("ACQUIRED", lease)
                return True
            if self._lease.expires_at - now <= self._lease_ttl / 2:
                renewed = await self._lease_port.renew(self._lease, self._lease_ttl)
                if renewed is None:
                    await self._lose_leadership("lease was not renewable")
                    return False
                self._lease = renewed
            return True
        except Exception:
            # If the lease cannot be proven, behave as a follower: never run jobs on an unverified lease.
            _log.exception("scheduler lease check failed")
            if self._lease is not None:
                await self._lose_leadership("lease backend error")
            return False

    async def _lose_leadership(self, reason: str) -> None:
        lost, self._lease = self._lease, None
        _log.warning("scheduler leadership lost", extra={"reason": reason})
        await self._cancel_running()
        if lost is not None:
            await self._record_leadership("LOST", lost, reason=reason)

    async def _cancel_running(self) -> None:
        tasks = [s.task for s in self._states.values() if s.task is not None and not s.task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _record_leadership(self, outcome: str, lease: Lease, *, reason: str | None = None) -> None:
        _log.info(
            "scheduler leadership",
            extra={"outcome": outcome, "holder_id": lease.holder_id, "fencing_token": lease.fencing_token},
        )
        details: dict[str, object] = {"holder_id": lease.holder_id, "fencing_token": lease.fencing_token}
        if reason:
            details["reason"] = reason
        await self._audit_safely("scheduler.leadership", outcome, details)

    # ------------------------------------------------------------------------------------------------- jobs
    async def _run(self, state: _JobState) -> None:
        spec = state.spec
        state.last_started_at = self._clock.now()
        state.run_count += 1
        timeout = spec.timeout or self._default_timeout
        error: str | None = None
        outcome = "ok"
        budget = asyncio.timeout(timeout.total_seconds() if timeout else None)
        try:
            async with budget:
                await spec.run()
        except asyncio.CancelledError:
            outcome = "cancelled"
            _log.info("scheduled job cancelled", extra={"job": spec.name})
            raise
        except TimeoutError as exc:
            if budget.expired():
                outcome, error = "timeout", self._describe(exc, f"exceeded {timeout}")
                _log.error("scheduled job timed out", extra={"job": spec.name, "timeout": str(timeout)})
            else:
                outcome, error = "failed", self._describe(exc)
                _log.exception("scheduled job failed", extra={"job": spec.name})
        except Exception as exc:
            outcome, error = "failed", self._describe(exc)
            _log.exception("scheduled job failed", extra={"job": spec.name})
        finally:
            state.last_finished_at = self._clock.now()
            state.last_outcome = outcome
            state.last_error = error
        await self._settle(state, failed=outcome in {"failed", "timeout"}, outcome=outcome, error=error)

    @staticmethod
    def _describe(exc: BaseException, fallback: str = "") -> str:
        text = redact_text(str(exc)).strip() or fallback
        return f"{type(exc).__name__}: {text}"[:_MAX_ERROR_TEXT]

    async def _settle(self, state: _JobState, *, failed: bool, outcome: str, error: str | None) -> None:
        if failed:
            state.failure_count += 1
            state.consecutive_failures += 1
        else:
            state.consecutive_failures = 0
        # Audit health *changes* only: a job that fails every five minutes must not flood the hash chain.
        if failed and not state.reported_failing:
            state.reported_failing = True
            await self._audit_safely(
                "scheduler.job", "FAILED", {"job": state.spec.name, "reason": outcome, "error": error}
            )
        elif not failed and state.reported_failing:
            state.reported_failing = False
            await self._audit_safely("scheduler.job", "RECOVERED", {"job": state.spec.name})

    async def _audit_safely(self, action: str, outcome: str, details: dict[str, object]) -> None:
        if self._audit is None:
            return
        try:
            await self._audit.record(actor=SYSTEM_PRINCIPAL.id, action=action, outcome=outcome, details=details)
        except Exception:
            _log.warning("scheduler audit record failed", exc_info=True)
