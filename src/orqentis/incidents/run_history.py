"""Reducing the recent runs of an item to what explains a failure (pure functions, no I/O).

A *run* is a job instance (pipeline, notebook, Spark job definition, Dataflow) or a semantic model refresh. Whatever
its source, it is reduced to a :class:`RunPoint`: when it ended, whether it succeeded or failed, and how long it took.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from statistics import median
from zoneinfo import ZoneInfo

from orqentis.domain.events import EventType, OperationalEvent
from orqentis.domain.runs import PipelineRun, Refresh, RunStatus

_SUCCESS_EVENTS = frozenset(
    {
        EventType.PIPELINE_RUN_SUCCEEDED,
        EventType.NOTEBOOK_RUN_SUCCEEDED,
        EventType.SPARK_JOB_SUCCEEDED,
        EventType.DATAFLOW_REFRESH_SUCCEEDED,
        EventType.REFRESH_SUCCEEDED,
    }
)
_FAILURE_EVENTS = frozenset(
    {
        EventType.PIPELINE_RUN_FAILED,
        EventType.NOTEBOOK_RUN_FAILED,
        EventType.SPARK_JOB_FAILED,
        EventType.DATAFLOW_REFRESH_FAILED,
        EventType.REFRESH_FAILED,
    }
)


@dataclass(frozen=True)
class RunPoint:
    """One finished run. ``ok`` is ``None`` for an outcome that is neither a success nor a failure (cancelled)."""

    started_at: datetime | None
    ended_at: datetime | None
    ok: bool | None
    duration_seconds: float | None = None
    run_id: str | None = None
    error: str | None = None

    @property
    def when(self) -> datetime | None:
        return self.ended_at or self.started_at


@dataclass(frozen=True)
class RunSummary:
    runs_considered: int
    last_success_at: datetime | None
    consecutive_failures: int
    first_failure_at: datetime | None
    median_duration_seconds: float | None


def _ok(status: RunStatus) -> bool | None:
    if status is RunStatus.SUCCEEDED:
        return True
    return False if status.is_failure else None


def points_from_runs(runs: Iterable[PipelineRun | Refresh]) -> list[RunPoint]:
    """Finished runs (jobs or refreshes), newest first. Runs still going, skipped or deduplicated are left out."""
    points = [
        RunPoint(
            r.start_time,
            r.end_time,
            _ok(r.status),
            r.duration_seconds,
            r.id,
            (
                r.failure.message
                if isinstance(r, PipelineRun) and r.failure
                else r.error.message
                if isinstance(r, Refresh) and r.error
                else None
            ),
        )
        for r in runs
        if r.status.is_terminal and r.status not in {RunStatus.SKIPPED, RunStatus.DEDUPED}
    ]
    return newest_first(points)


def points_from_events(events: Iterable[OperationalEvent]) -> list[RunPoint]:
    """The same from stored events (their payloads carry status and duration), newest first."""
    points: list[RunPoint] = []
    for e in events:
        if e.event_type not in _SUCCESS_EVENTS | _FAILURE_EVENTS:
            continue
        duration = e.payload.get("duration_seconds")
        points.append(
            RunPoint(
                None,
                e.timestamp,
                e.event_type in _SUCCESS_EVENTS,
                float(duration) if isinstance(duration, int | float) else None,
                str(e.payload.get("refresh_id") or e.payload.get("run_id") or e.event_id),
                e.payload.get("error_message") if isinstance(e.payload.get("error_message"), str) else None,
            )
        )
    return newest_first(points)


def newest_first(points: Iterable[RunPoint]) -> list[RunPoint]:
    return sorted(points, key=lambda p: p.when.timestamp() if p.when else 0.0, reverse=True)


def summarize(points: Sequence[RunPoint]) -> RunSummary:
    """``points`` newest first. Cancelled runs neither extend nor break a streak of failures."""
    decided = [p for p in points if p.ok is not None]
    streak: list[RunPoint] = []
    for point in decided:
        if point.ok:
            break
        streak.append(point)
    success = next((p for p in decided if p.ok), None)
    durations = [p.duration_seconds for p in decided if p.ok and p.duration_seconds is not None]
    return RunSummary(
        runs_considered=len(points),
        last_success_at=success.when if success else None,
        consecutive_failures=len(streak),
        first_failure_at=(streak[-1].started_at or streak[-1].when) if streak else None,
        median_duration_seconds=float(median(durations)) if durations else None,
    )


def usual_start_local(points: Sequence[RunPoint], zone: str) -> str | None:
    """The median time of day, in ``zone``, at which the successful runs started (``HH:MM``), if known."""
    tz = ZoneInfo(zone)
    minutes = sorted(
        local.hour * 60 + local.minute
        for p in points
        if p.ok and p.started_at is not None
        for local in (p.started_at.astimezone(tz),)
    )
    if not minutes:
        return None
    mid = minutes[len(minutes) // 2]
    return f"{mid // 60:02d}:{mid % 60:02d}"
