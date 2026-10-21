"""How an item has been running: the reduction of recent runs that explains a failure, and when it usually starts."""

from __future__ import annotations

from datetime import datetime, timedelta

from orqentis.domain.events import EventType
from orqentis.domain.runs import RunStatus
from orqentis.incidents.run_history import (
    RunPoint,
    points_from_events,
    points_from_runs,
    summarize,
    usual_start_local,
)
from tests.support.signals import at, event, job_run, refresh_run


def point(ok: bool | None, hour: int, *, minutes: float = 10, day: int = 9) -> RunPoint:
    start = at(hour, day=day)
    end = start + timedelta(minutes=minutes)
    return RunPoint(start, end, ok, minutes * 60, f"r-{day}-{hour}")


def test_a_streak_of_failures_ends_at_the_last_success() -> None:
    points = [point(False, 10), point(False, 9), point(True, 8), point(False, 7)]
    summary = summarize(points)
    assert summary.consecutive_failures == 2 and summary.runs_considered == 4
    assert summary.last_success_at == at(8, 10) and summary.first_failure_at == at(9)


def test_cancelled_runs_neither_extend_nor_break_a_streak() -> None:
    summary = summarize([point(False, 10), point(None, 9), point(False, 8), point(True, 7)])
    assert summary.consecutive_failures == 2


def test_no_failure_means_no_streak_and_no_runs_means_nothing_known() -> None:
    assert summarize([point(True, 10)]).consecutive_failures == 0
    empty = summarize([])
    assert (
        empty.runs_considered,
        empty.last_success_at,
        empty.consecutive_failures,
        empty.median_duration_seconds,
    ) == (0, None, 0, None)


def test_the_median_duration_is_of_the_successes() -> None:
    summary = summarize(
        [
            point(False, 10, minutes=1),
            point(True, 9, minutes=10),
            point(True, 8, minutes=20),
            point(True, 7, minutes=30),
        ]
    )
    assert summary.median_duration_seconds == 20 * 60


def test_jobs_become_points_newest_first_and_unfinished_runs_are_left_out() -> None:
    runs = [
        job_run("old", "nb-1", RunStatus.SUCCEEDED, at(6), at(6, 5)),
        job_run("bad", "nb-1", RunStatus.FAILED, at(11), at(11, 3), message="ValueError: x"),
        job_run("going", "nb-1", RunStatus.IN_PROGRESS, at(11, 30), at(11, 35)),
        job_run("skipped", "nb-1", RunStatus.SKIPPED, at(11, 40), at(11, 41)),
    ]
    points = points_from_runs(runs)
    assert [p.run_id for p in points] == ["bad", "old"]
    assert points[0].ok is False and points[0].error == "ValueError: x" and points[1].ok is True


def test_refreshes_become_points_with_their_error() -> None:
    points = points_from_runs(
        [refresh_run("ref-1", "sm-1", RunStatus.FAILED, at(11), at(11, 2), message="cannot access")]
    )
    assert points[0].ok is False and points[0].error == "cannot access" and points[0].duration_seconds == 120


def test_stored_events_become_points_too() -> None:
    events = [
        event(
            EventType.REFRESH_FAILED,
            when=at(11),
            payload={"refresh_id": "ref-2", "error_message": "boom", "duration_seconds": 30},
        ),
        event(EventType.REFRESH_SUCCEEDED, when=at(9), payload={"refresh_id": "ref-1", "duration_seconds": "n/a"}),
        event(EventType.INCIDENT_CREATED, when=at(10)),  # not a run
    ]
    points = points_from_events(events)
    assert [(p.run_id, p.ok, p.error) for p in points] == [("ref-2", False, "boom"), ("ref-1", True, None)]
    assert points[0].duration_seconds == 30.0 and points[1].duration_seconds is None


def test_the_usual_start_is_the_median_time_of_day_in_the_slas_zone() -> None:
    def started(hour: int, minute: int, day: int) -> RunPoint:
        start = datetime(2026, 10, day, hour, minute, tzinfo=at(0).tzinfo)
        return RunPoint(start, start + timedelta(minutes=10), True, 600.0)

    points = [started(19, 0, 2), started(19, 5, 3), started(19, 10, 4), RunPoint(None, None, True)]
    assert usual_start_local(points, "Australia/Sydney") == "06:05"  # 19:05 UTC is 06:05 the next day in AEDT
    assert usual_start_local(points, "UTC") == "19:05"
    assert usual_start_local([point(False, 5)], "UTC") is None
    assert usual_start_local([], "UTC") is None
