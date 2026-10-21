"""A breach is never declared from blindness (ADR-0013, open question 3): the collector that observes the freshness item
must have completed a pass for it recently, or the sweep withholds the breach until it has."""

from __future__ import annotations

from datetime import timedelta

import pytest

from orqentis.bootstrap import Container
from orqentis.config.clock import FixedClock
from orqentis.domain.common import Environment
from orqentis.domain.events import EventType
from orqentis.domain.sla import SLA, SLAState
from orqentis.incidents.detection import DetectionEngine
from orqentis.incidents.detection_runner import DetectionRunner
from orqentis.incidents.sla import SLAEvaluator
from orqentis.incidents.sla_sweep import SLASweeper
from orqentis.storage.event_store import EventQuery

LAG = timedelta(minutes=30)
SALES = "pl-ingest-sales-prod"


def sweeper(container: Container, lag: timedelta | None = LAG) -> SLASweeper:
    clock = container.clock
    assert isinstance(clock, FixedClock)
    return SLASweeper(
        container.catalog,
        SLAEvaluator(container.event_store, clock),
        container.graph,
        container.incidents.repository,
        container.event_store,
        DetectionRunner(
            container.event_store,
            DetectionEngine(container.incidents, clock, events=container.event_store),
            consumer="gate",
        ),
        clock,
        max_telemetry_lag=lag,
    )


async def breached_events(container: Container) -> list[str]:
    events = await container.event_store.query(EventQuery(event_types=[EventType.SLA_ASSESSED]))
    return sorted(str(e.payload["sla_id"]) for e in events if e.payload["state"] == "BREACHED")


async def past_the_deadline(container: Container) -> FixedClock:
    clock = container.clock
    assert isinstance(clock, FixedClock)
    clock.advance(timedelta(hours=1))  # 07:05: the 06:30 and 07:00 deadlines have passed
    return clock


async def test_a_breach_is_withheld_while_nothing_has_been_collected(container: Container) -> None:
    await past_the_deadline(container)
    assessments, created = await sweeper(container).sweep()
    assert SLAState.BREACHED in {a.state for a in assessments}  # the assessment is returned ...
    assert await breached_events(container) == [] and created == []  # ... but no event, so no incident


async def test_a_fresh_watermark_lets_the_breach_through(container: Container) -> None:
    clock = await past_the_deadline(container)
    await container.event_store.advance_watermarks("pipeline-runs", {SALES: clock.now() - timedelta(minutes=5)})
    _, created = await sweeper(container).sweep()
    assert await breached_events(container) == ["sla-sales-gold-0630"]  # only the one with current telemetry
    assert [i.metadata["detection_rule"] for i in created] == ["DET-SLA-BREACHED"]


async def test_a_stale_watermark_is_blindness_too(container: Container) -> None:
    clock = await past_the_deadline(container)
    await container.event_store.advance_watermarks("pipeline-runs", {SALES: clock.now() - LAG - timedelta(minutes=1)})
    await sweeper(container).sweep()
    assert await breached_events(container) == []


async def test_the_breach_arrives_once_telemetry_is_current_again(container: Container) -> None:
    clock = await past_the_deadline(container)
    await sweeper(container).sweep()
    assert await breached_events(container) == []
    await container.event_store.advance_watermarks("pipeline-runs", {SALES: clock.now()})
    await sweeper(container).sweep()
    assert await breached_events(container) == ["sla-sales-gold-0630"]


async def test_without_a_lag_the_check_is_off(container: Container) -> None:
    await past_the_deadline(container)
    await sweeper(container, lag=None).sweep()
    # with nothing collected every SLA reads as breached; the check is what stops that from being declared
    assert await breached_events(container) == ["sla-gl-daily-0700", "sla-inventory-0700", "sla-sales-gold-0630"]


@pytest.mark.parametrize(
    ("collector", "scope"),
    [("semantic-model-refreshes", "sm-sales"), ("workspace-monitoring-refreshes", "ws-sales-prod")],
)
async def test_a_semantic_model_is_observed_by_whichever_refresh_source_is_configured(
    container: Container, collector: str, scope: str
) -> None:
    clock = await past_the_deadline(container)
    container.catalog.slas["sla-model"] = SLA(
        id="sla-model",
        tenant_id="tenant-contoso",
        data_product_id="dp-sales-gold",
        display_name="model",
        description="the model must be fresh",
        deadline_local_time=container.catalog.slas["sla-sales-gold-0630"].deadline_local_time,
        timezone="Australia/Sydney",
        freshness_item_id="sm-sales",
        owner="o",
    )
    await sweeper(container).sweep()
    assert "sla-model" not in await breached_events(container)  # nothing observes sm-sales yet
    await container.event_store.advance_watermarks(collector, {scope: clock.now()})
    await sweeper(container).sweep()
    assert "sla-model" in await breached_events(container)
    assert (
        container.catalog.node("sm-sales") is not None
        and container.catalog.node("sm-sales").environment is Environment.PROD
    )  # type: ignore[union-attr]


async def test_an_item_nothing_collects_has_no_watermark_to_wait_for(container: Container) -> None:
    await past_the_deadline(container)
    container.catalog.slas["sla-lake"] = container.catalog.slas["sla-sales-gold-0630"].model_copy(
        update={"id": "sla-lake", "freshness_item_id": "lh-sales-gold"}
    )
    await sweeper(container).sweep()
    assert "sla-lake" in await breached_events(container)
