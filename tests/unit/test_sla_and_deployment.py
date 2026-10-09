"""SLA evaluation and deployment comparison."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from orqentis.config.clock import FixedClock
from orqentis.domain.common import Environment, ItemType
from orqentis.domain.deployment import DeploymentState
from orqentis.domain.events import EventType, OperationalEvent
from orqentis.domain.sla import SLA, SLAState
from orqentis.incidents.deployment import compare_deployments
from orqentis.incidents.sla import SLAEvaluator
from orqentis.storage.event_store import LocalEventStore
from orqentis.storage.sqlite import SqliteDatabase

SYD = ZoneInfo("Australia/Sydney")
SLA_0630 = SLA(
    id="sla",
    tenant_id="t",
    data_product_id="dp",
    display_name="x",
    description="before 06:30",
    deadline_local_time=time(6, 30),
    timezone="Australia/Sydney",
    freshness_item_id="pl",
    owner="o",
    applies_on_weekdays=[1, 2, 3, 4, 5],
)


def success(at: datetime) -> OperationalEvent:
    return OperationalEvent(
        event_id=f"e{at.timestamp()}",
        event_type=EventType.PIPELINE_RUN_SUCCEEDED,
        tenant_id="t",
        item_id="pl",
        timestamp=at,
        summary="ok",
        dedupe_key=f"k{at.timestamp()}",
        source="test",
    )


async def evaluate(tmp_path: Path, now: datetime, successes: list[datetime], blocking: list[str]) -> SLAState:
    store = LocalEventStore(SqliteDatabase(tmp_path / "e.sqlite3"))
    await store.append([success(s) for s in successes])
    return (
        await SLAEvaluator(store, FixedClock(now)).assess(SLA_0630, data_product_name="dp", blocking_item_ids=blocking)
    ).state


MONDAY = datetime(2026, 10, 5, tzinfo=SYD)


@pytest.mark.parametrize(
    ("now", "successes", "blocking", "state"),
    [
        (MONDAY.replace(hour=6, minute=20), [MONDAY.replace(hour=6, minute=10)], [], SLAState.MET),
        (
            MONDAY.replace(hour=6, minute=5),
            [MONDAY.replace(hour=6, minute=10) - timedelta(days=1)],
            ["pl"],
            SLAState.AT_RISK,
        ),
        (
            MONDAY.replace(hour=6, minute=5),
            [MONDAY.replace(hour=6, minute=10) - timedelta(days=1)],
            [],
            SLAState.ON_TRACK,
        ),
        (MONDAY.replace(hour=7), [MONDAY.replace(hour=6, minute=10) - timedelta(days=1)], ["pl"], SLAState.BREACHED),
        (MONDAY.replace(hour=7), [MONDAY.replace(hour=6, minute=45)], [], SLAState.BREACHED),  # late refresh
        (MONDAY.replace(hour=7), [], [], SLAState.BREACHED),
        (MONDAY.replace(day=4, hour=6), [], [], SLAState.NOT_APPLICABLE),  # Sunday
    ],
)
async def test_sla_states(
    tmp_path: Path, now: datetime, successes: list[datetime], blocking: list[str], state: SLAState
) -> None:
    assert await evaluate(tmp_path, now, successes, blocking) is state


def dstate(env: Environment, commit: str, at: datetime, item: str) -> DeploymentState:
    return DeploymentState(
        id=f"d-{env}",
        tenant_id="t",
        logical_item_id="transform",
        item_id=item,
        workspace_id="w",
        item_type=ItemType.NOTEBOOK,
        display_name="transform",
        environment=env,
        deployed_at=at,
        commit_id=commit,
        config_hash="cfg",
    )


def test_deployment_comparison_no_drift_not_recent() -> None:
    states = [
        dstate(Environment.DEV, "c2", MONDAY - timedelta(days=3), "nb-dev"),
        dstate(Environment.TEST, "c1", MONDAY - timedelta(days=7), "nb-test"),
        dstate(Environment.PROD, "c1", MONDAY - timedelta(days=7), "nb"),
    ]
    cmp = compare_deployments("nb", states, reference_time=MONDAY)
    assert cmp is not None
    assert cmp.drift_detected is False
    assert cmp.changed_within_window is False
    assert any("DEV commit c2 differs from TEST" in d for d in cmp.differences)


def test_deployment_comparison_detects_drift_and_recent_change() -> None:
    states = [
        dstate(Environment.TEST, "c1", MONDAY - timedelta(days=7), "nb-test"),
        dstate(Environment.PROD, "c9", MONDAY - timedelta(hours=2), "nb"),
    ]
    cmp = compare_deployments("nb", states, reference_time=MONDAY)
    assert cmp is not None and cmp.drift_detected and cmp.changed_within_window


def test_deployment_comparison_unknown_item() -> None:
    assert compare_deployments("nope", [], reference_time=MONDAY) is None
