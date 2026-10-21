"""The WS7 detection rules and how the engine keeps one failure to one incident (ADR-0020)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from orqentis.bootstrap import Container
from orqentis.config.clock import FixedClock
from orqentis.domain.common import Environment, ItemType
from orqentis.domain.events import EventType, OperationalEvent
from orqentis.domain.taxonomy import IncidentCategory, Severity
from orqentis.incidents.detection import (
    COVERAGE_WINDOW,
    DEFAULT_RULES,
    DataflowFailureRule,
    DetectionEngine,
    NotebookFailureRule,
    RefreshFailureRule,
    SlaBreachRule,
    SparkJobFailureRule,
)
from tests.support.signals import event


def test_the_default_rules_cover_every_signal_source() -> None:
    assert [r.rule_id for r in DEFAULT_RULES] == [
        "DET-PIPELINE-FAILED",
        "DET-NOTEBOOK-FAILED",
        "DET-SPARK-JOB-FAILED",
        "DET-DATAFLOW-FAILED",
        "DET-REFRESH-FAILED",
        "DET-SLA-BREACHED",
    ]


@pytest.mark.parametrize(
    ("rule", "event_type", "item_type", "title"),
    [
        (NotebookFailureRule(), EventType.NOTEBOOK_RUN_FAILED, ItemType.NOTEBOOK, "Notebook transform failed"),
        (
            SparkJobFailureRule(),
            EventType.SPARK_JOB_FAILED,
            ItemType.SPARK_JOB_DEFINITION,
            "Spark job definition transform failed",
        ),
        (DataflowFailureRule(), EventType.DATAFLOW_REFRESH_FAILED, ItemType.DATAFLOW, "Dataflow transform failed"),
    ],
)
def test_a_job_that_failed_on_its_own_opens_an_activity_failure(
    rule: Any, event_type: EventType, item_type: ItemType, title: str
) -> None:
    candidate = rule.evaluate(event(event_type, item_type=item_type, payload={"launched_by_item": False}))
    assert candidate is not None
    assert candidate.title == title and candidate.category == IncidentCategory.ACTIVITY_FAILURE
    assert candidate.severity is Severity.HIGH  # PROD
    assert candidate.primary_item_type is item_type and candidate.correlation_id == "run-1"
    assert candidate.rule_id == rule.rule_id and candidate.trigger_event_id.startswith("evt-")
    assert candidate.covered_by_activity_target == "nb-1"
    assert [s.event_id for s in candidate.symptoms] == [candidate.trigger_event_id]


@pytest.mark.parametrize("environment", list(Environment))
def test_severity_follows_the_environment(environment: Environment) -> None:
    expected = {
        Environment.PROD: Severity.HIGH,
        Environment.TEST: Severity.MEDIUM,
        Environment.DEV: Severity.LOW,
        Environment.UNKNOWN: Severity.MEDIUM,
    }
    candidate = NotebookFailureRule().evaluate(event(EventType.NOTEBOOK_RUN_FAILED, environment=environment))
    assert candidate is not None and candidate.severity is expected[environment]


def test_a_job_started_by_another_item_is_that_items_failure() -> None:
    started_by_pipeline = event(EventType.NOTEBOOK_RUN_FAILED, payload={"launched_by_item": True})
    assert NotebookFailureRule().evaluate(started_by_pipeline) is None


@pytest.mark.parametrize(
    "bad",
    [
        event(EventType.NOTEBOOK_RUN_SUCCEEDED),
        event(EventType.SPARK_JOB_FAILED),  # another rule's event
        event(EventType.NOTEBOOK_RUN_FAILED, workspace_id=None),
    ],
)
def test_other_events_do_not_open_a_notebook_incident(bad: OperationalEvent) -> None:
    assert NotebookFailureRule().evaluate(bad) is None


def test_a_failed_refresh_opens_a_model_incident_and_a_success_does_not() -> None:
    failed = event(
        EventType.REFRESH_FAILED,
        item_id="sm-1",
        item_type=ItemType.SEMANTIC_MODEL,
        name="SalesModel",
        correlation_id="ref-1",
    )
    candidate = RefreshFailureRule().evaluate(failed)
    assert candidate is not None
    assert candidate.category == IncidentCategory.SEMANTIC_MODEL_REFRESH_FAILURE
    assert (
        candidate.title == "Semantic model SalesModel refresh failed" and candidate.covered_by_activity_target == "sm-1"
    )
    assert RefreshFailureRule().evaluate(event(EventType.REFRESH_SUCCEEDED, item_id="sm-1")) is None


def sla_event(
    *,
    state: str = "BREACHED",
    environment: Environment = Environment.PROD,
    declared: Any = None,
    correlation: str | None = "sla:s1:2026-10-09",
) -> OperationalEvent:
    payload: dict[str, Any] = {"state": state, "data_product_name": "sales_gold", "sla_id": "s1"}
    if declared is not None:
        payload["declared"] = declared
    return event(
        EventType.SLA_ASSESSED,
        item_id="pl-1",
        item_type=ItemType.DATA_PIPELINE,
        environment=environment,
        payload=payload,
        correlation_id=correlation,
        name="sales_gold",
    )


def test_a_breached_sla_opens_an_sla_incident_for_the_freshness_item() -> None:
    candidate = SlaBreachRule().evaluate(sla_event(declared={"freshness_item_name": "ingest_sales"}))
    assert candidate is not None
    assert candidate.title == "SLA breached: sales_gold" and candidate.category == IncidentCategory.SLA_BREACH
    assert candidate.primary_item_id == "pl-1" and candidate.primary_item_type is ItemType.DATA_PIPELINE
    assert candidate.primary_item_name == "ingest_sales" and candidate.correlation_id == "sla:s1:2026-10-09"
    assert candidate.covered_by_activity_target is None  # an SLA breach is its own incident


@pytest.mark.parametrize("state", ["MET", "ON_TRACK", "AT_RISK", "NOT_APPLICABLE", "UNKNOWN"])
def test_only_a_breach_opens_an_incident(state: str) -> None:
    assert SlaBreachRule().evaluate(sla_event(state=state)) is None


def test_an_sla_event_without_what_a_rule_needs_is_ignored() -> None:
    assert SlaBreachRule().evaluate(sla_event(correlation=None)) is None


@pytest.mark.parametrize(
    ("environment", "declared", "expected"),
    [
        (Environment.PROD, None, Severity.CRITICAL),
        (Environment.TEST, None, Severity.MEDIUM),
        (Environment.DEV, None, Severity.LOW),
        (Environment.UNKNOWN, None, Severity.MEDIUM),
        (Environment.DEV, {"criticality": "critical"}, Severity.CRITICAL),  # the owner's declaration wins ...
        (Environment.PROD, {"criticality": "low"}, Severity.LOW),
        (Environment.PROD, {"criticality": " High "}, Severity.HIGH),
        (Environment.PROD, {"criticality": "urgent!!"}, Severity.CRITICAL),  # ... when it is one we know
        (Environment.TEST, {"criticality": 5}, Severity.MEDIUM),
    ],
)
def test_sla_severity_comes_from_the_declared_criticality_then_the_environment(
    environment: Environment, declared: Any, expected: Severity
) -> None:
    candidate = SlaBreachRule().evaluate(sla_event(environment=environment, declared=declared))
    assert candidate is not None and candidate.severity is expected


# --------------------------------------------------------------------------------------------- the engine
async def process(container: Container, *events: OperationalEvent) -> list[Any]:
    await container.event_store.append(list(events))
    engine = DetectionEngine(container.incidents, container.clock, events=container.event_store)
    return await engine.process(list(events))


def around(container: Container, **offset: float) -> Any:
    return container.clock.now() + timedelta(**offset)


def activity_failure(
    container: Container, *, target: str, when: Any, item_id: str = "pl-ingest-inventory-prod"
) -> OperationalEvent:
    return event(
        EventType.ACTIVITY_RUN_FAILED,
        item_id=item_id,
        item_type=ItemType.DATA_PIPELINE,
        when=when,
        correlation_id="pipeline-run-1",
        payload={"target_item_id": target, "activity_run_id": "act-1"},
        event_id="evt-act-1",
    )


async def test_a_notebook_that_failed_on_its_own_opens_exactly_one_incident(container: Container) -> None:
    failed = event(
        EventType.NOTEBOOK_RUN_FAILED,
        item_id="nb-inventory-snapshot",
        when=around(container, minutes=-5),
        workspace_id="ws-supply-prod",
        payload={"launched_by_item": False},
    )
    created = await process(container, failed)
    assert len(created) == 1
    incident = created[0]
    assert (
        incident.primary_item_type is ItemType.NOTEBOOK and incident.metadata["detection_rule"] == "DET-NOTEBOOK-FAILED"
    )
    assert incident.correlation_id == "run-1"
    again = await process(container, failed)  # the cursor re-reads events after an interruption: harmless
    assert again == []


async def test_a_pipeline_failure_covers_the_notebook_it_ran(container: Container) -> None:
    when = around(container, minutes=-5)
    cover = activity_failure(container, target="nb-inventory-snapshot", when=when + timedelta(seconds=3))
    notebook_failed = event(
        EventType.NOTEBOOK_RUN_FAILED,
        item_id="nb-inventory-snapshot",
        when=when,
        workspace_id="ws-supply-prod",
        payload={"launched_by_item": False},  # the API does not say that a pipeline started it
    )
    assert await process(container, cover, notebook_failed) == []


async def test_the_coverage_is_the_target_and_the_time_never_a_name(container: Container) -> None:
    when = around(container, minutes=-5)
    notebook_failed = event(
        EventType.NOTEBOOK_RUN_FAILED,
        item_id="nb-inventory-snapshot",
        when=when,
        workspace_id="ws-supply-prod",
        payload={},
    )
    elsewhere = activity_failure(container, target="nb-transform-sales", when=when)  # another notebook's failure
    assert len(await process(container, elsewhere, notebook_failed)) == 1


async def test_a_pipeline_failure_long_before_does_not_cover_a_new_failure(container: Container) -> None:
    when = around(container, minutes=-5)
    long_before = activity_failure(
        container, target="nb-inventory-snapshot", when=when - COVERAGE_WINDOW - timedelta(minutes=1)
    )
    notebook_failed = event(
        EventType.NOTEBOOK_RUN_FAILED,
        item_id="nb-inventory-snapshot",
        when=when,
        workspace_id="ws-supply-prod",
        payload={},
    )
    assert len(await process(container, long_before, notebook_failed)) == 1


async def test_a_refresh_failure_is_covered_by_the_pipeline_whose_refresh_activity_failed(container: Container) -> None:
    when = around(container, minutes=-5)
    cover = activity_failure(container, target="sm-sales", when=when, item_id="pl-ingest-sales-prod")
    refresh_failed = event(
        EventType.REFRESH_FAILED,
        item_id="sm-sales",
        item_type=ItemType.SEMANTIC_MODEL,
        when=when,
        workspace_id="ws-sales-prod",
        correlation_id="ref-1",
        payload={"refresh_id": "ref-1"},
        name="SalesSemanticModel",
    )
    assert await process(container, cover, refresh_failed) == []
    alone = event(
        EventType.REFRESH_FAILED,
        item_id="sm-sales",
        item_type=ItemType.SEMANTIC_MODEL,
        when=when,
        workspace_id="ws-sales-prod",
        correlation_id="ref-2",
        payload={"refresh_id": "ref-2"},
        name="SalesSemanticModel",
        event_id="evt-ref-2",
    )
    # the pipeline's activity is still within the window, so the second refresh failure is covered too; a new
    # failure well outside the window is not
    later = alone.model_copy(
        update={
            "timestamp": when + COVERAGE_WINDOW + timedelta(minutes=2),
            "event_id": "evt-ref-3",
            "correlation_id": "ref-3",
            "dedupe_key": "x3",
        }
    )
    assert len(await process(container, later)) == 1


async def test_an_sla_breach_is_not_covered_by_a_pipeline_failure(container: Container) -> None:
    when = around(container, minutes=-5)
    cover = activity_failure(container, target="pl-ingest-sales-prod", when=when)
    breach = sla_event().model_copy(
        update={"timestamp": when, "item_id": "pl-ingest-sales-prod", "workspace_id": "ws-sales-prod"}
    )
    created = await process(container, cover, breach)
    assert [i.category for i in created] == [IncidentCategory.SLA_BREACH]  # the business impact is tracked on its own


async def test_the_engine_does_not_look_for_coverage_without_an_event_store(container: Container) -> None:
    when = around(container, minutes=-5)
    engine = DetectionEngine(container.incidents, FixedClock(container.clock.now()))  # no store to look in
    failed = event(
        EventType.NOTEBOOK_RUN_FAILED,
        item_id="nb-inventory-snapshot",
        when=when,
        workspace_id="ws-supply-prod",
        payload={},
    )
    assert len(await engine.process([failed])) == 1
