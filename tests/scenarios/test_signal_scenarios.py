"""WS7 scenarios: every injected failure class is detected by a deterministic rule and investigated to the right category.

Each scenario is synthetic telemetry (fixtures/telemetry) played through the real collectors, rules, workflows,
runbooks and policy. The four original scenarios live in test_scenarios.py and are unchanged.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import pytest

from orqentis.bootstrap import Container, build_container
from orqentis.domain.confidence import ConfidenceLevel
from orqentis.domain.events import EventType
from orqentis.domain.evidence import EvidenceType
from orqentis.domain.incidents import ApprovalState, Incident, IncidentStatus
from orqentis.domain.taxonomy import Severity
from orqentis.fabric.fixtures import list_scenarios
from orqentis.storage.event_store import EventQuery
from tests.conftest import OPERATOR, REPO, detect_and_investigate, make_settings

pytestmark = pytest.mark.scenario


@pytest.fixture
async def scenario(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[Container]:
    c = await build_container(make_settings(tmp_path, scenario=request.param), reset=True)
    try:
        yield c
    finally:
        await c.aclose()


def statements(incident: Incident) -> str:
    return " | ".join(f.statement for f in incident.findings)


def assert_grounded(incident: Incident) -> None:
    """Every finding cites evidence that exists on the incident, and so does the root cause."""
    known = incident.evidence_by_id()
    assert incident.findings and all(
        f.evidence_ids and all(e in known for e in f.evidence_ids) for f in incident.findings
    )
    for cause in incident.probable_root_causes:
        assert cause.evidence_ids and all(e in known for e in cause.evidence_ids)


def test_every_scenario_declares_what_it_expects() -> None:
    names = {s.name for s in list_scenarios(REPO / "fixtures")}
    assert {
        "notebook_failure",
        "refresh_failure",
        "refresh_failure_pipeline",
        "sla_breach",
        "schema_drift_delta",
    } <= names
    assert {"schema_drift", "auth_failure", "capacity_pressure", "unknown_failure"} <= names


# --------------------------------------------------------------------------------------- notebook failure
@pytest.mark.parametrize("scenario", ["notebook_failure"], indirect=True)
async def test_a_notebook_that_raised_is_an_activity_failure_with_an_honest_confidence(scenario: Container) -> None:
    incident = await detect_and_investigate(scenario)
    assert incident.metadata["detection_rule"] == "DET-NOTEBOOK-FAILED" and incident.severity is Severity.HIGH
    assert incident.primary_item_id == "nb-inventory-snapshot" and incident.status is IncidentStatus.DIAGNOSED
    assert incident.category == "ACTIVITY_FAILURE"  # the live run left this UNKNOWN
    root = incident.probable_root_causes[0]
    assert "ValueError" in root.statement and "unit_cost" in root.statement and "own code or data" in root.statement
    # one source, no history, no upstream correlation: the model says so instead of sounding sure
    assert incident.confidence is not None and incident.confidence.level is ConfidenceLevel.LOW
    assert incident.matched_runbooks[0].runbook_id == "RB-NOTEBOOK-FAILURE-001"
    actions = incident.recommended_actions[0].actions
    assert {a.action_type.value for a in actions} == {"INSPECT_LOGS", "MANUAL_STEP"}
    assert not incident.recommended_actions[0].approval_required and incident.approval_ids == []
    assert incident.approval_state is ApprovalState.NOT_REQUIRED
    assert "failed 1 time(s) in a row" in statements(incident)
    types = {e.evidence_type for e in incident.evidence}
    assert {EvidenceType.JOB_FAILURE, EvidenceType.RUN_HISTORY, EvidenceType.ERROR_MESSAGE} <= types
    signature = next(e for e in incident.evidence if e.evidence_type is EvidenceType.ERROR_MESSAGE).structured_payload
    assert signature["signature_match"]["signature_id"] == "SIG-NOTEBOOK-EXCEPTION"  # no longer redacted away
    assert {a.display_name for a in incident.affected_assets} >= {"InventorySemanticModel", "InventoryHealthReport"}
    assert_grounded(incident)
    assert (await scenario.audit.verify()).valid


# ---------------------------------------------------------------------------------------- refresh failure
@pytest.mark.parametrize("scenario", ["refresh_failure"], indirect=True)
async def test_a_failed_refresh_opens_a_model_incident_and_reaches_the_refresh_runbook(scenario: Container) -> None:
    incident = await detect_and_investigate(scenario)
    assert incident.metadata["detection_rule"] == "DET-REFRESH-FAILED" and incident.primary_item_id == "sm-marketing"
    assert incident.category == "SEMANTIC_MODEL_REFRESH_FAILURE" and incident.status is IncidentStatus.AWAITING_APPROVAL
    root = incident.probable_root_causes[0]
    assert "campaigns_clean" in root.statement and "CampaignSemanticModel" in root.statement
    assert incident.confidence is not None and incident.confidence.level is ConfidenceLevel.LOW
    assert incident.matched_runbooks[0].runbook_id == "RB-SM-REFRESH-FAILURE-001"
    (action,) = incident.recommended_actions[0].actions
    assert (action.action_type.value, action.target_item_id) == ("REFRESH_SEMANTIC_MODEL", "sm-marketing")
    assert len(incident.approval_ids) == 1 and incident.approval_state is ApprovalState.PENDING
    assert {a.display_name for a in incident.affected_assets} == {"CampaignPerformanceReport"}
    refresh_evidence = next(e for e in incident.evidence if e.evidence_type is EvidenceType.SEMANTIC_MODEL_REFRESH)
    assert refresh_evidence.structured_payload["status"] == "Failed"
    assert_grounded(incident)


@pytest.mark.parametrize("scenario", ["refresh_failure_pipeline"], indirect=True)
async def test_a_refresh_that_fails_inside_a_pipeline_is_one_incident_in_the_same_category(scenario: Container) -> None:
    incident = await detect_and_investigate(scenario)
    # the model's own refresh failure was recorded too, but the pipeline's incident covers it
    stored = await scenario.event_store.query(EventQuery(event_types=[EventType.REFRESH_FAILED]))
    assert [e.item_id for e in stored] == ["sm-sales"]
    assert [i.incident_id for i in await scenario.incidents.repository.list()].count(incident.incident_id) == 1
    assert (
        incident.metadata["detection_rule"] == "DET-PIPELINE-FAILED"
        and incident.primary_item_id == "pl-ingest-sales-prod"
    )
    assert incident.category == "SEMANTIC_MODEL_REFRESH_FAILURE"
    assert "SalesSemanticModel" in incident.probable_root_causes[0].statement  # the model, not the pipeline
    assert incident.matched_runbooks[0].runbook_id == "RB-SM-REFRESH-FAILURE-001"
    (action,) = incident.recommended_actions[0].actions
    assert (action.action_type.value, action.target_item_id) == ("REFRESH_SEMANTIC_MODEL", "sm-sales")
    assert_grounded(incident)


# -------------------------------------------------------------------------------------------- SLA breach
@pytest.mark.parametrize("scenario", ["sla_breach"], indirect=True)
async def test_a_late_refresh_breaches_the_sla_and_the_investigation_says_how_late(scenario: Container) -> None:
    incident = await detect_and_investigate(scenario)
    assert incident.metadata["detection_rule"] == "DET-SLA-BREACHED" and incident.severity is Severity.CRITICAL
    assert incident.category == "SLA_BREACH" and incident.primary_item_id == "pl-ingest-finance-prod"
    assert incident.correlation_id == "sla:sla-gl-daily-0700:2026-10-05"
    root = incident.probable_root_causes[0]
    assert "gl_daily_balances missed its" in root.statement and "10 min late" in root.statement
    assert "06:48" in root.statement and "usually around 05:00" in root.statement and "took 21 min" in root.statement
    assert incident.confidence is not None and incident.confidence.level is ConfidenceLevel.MEDIUM
    assert incident.matched_runbooks[0].runbook_id == "RB-SLA-BREACH-001"
    (action,) = incident.recommended_actions[0].actions
    assert action.action_type.value == "MANUAL_STEP" and incident.approval_ids == []  # nothing to mutate
    assert [a.state.value for a in incident.sla_impact if a.sla_id == "sla-gl-daily-0700"] == ["BREACHED"]
    assert_grounded(incident)


@pytest.mark.parametrize("scenario", ["sla_breach"], indirect=True)
async def test_the_recorded_breach_is_what_the_sweep_itself_would_record(scenario: Container) -> None:
    """The scenario hands the breach over as an event; this guards that it is the event the real sweep produces."""
    recorded = next(e for e in scenario.scenario.events if e.event_type is EventType.SLA_ASSESSED)  # type: ignore[union-attr]
    await scenario.collect_and_detect()
    swept = (await scenario.sla_sweeper.sweep())[0]
    live = next(a for a in swept if a.sla_id == "sla-gl-daily-0700")
    assert live.model_dump(mode="json") == {k: v for k, v in recorded.payload.items() if k != "declared"}
    stored = {e.dedupe_key for e in await scenario.event_store.query(EventQuery(event_types=[EventType.SLA_ASSESSED]))}
    assert recorded.dedupe_key in stored  # the sweep found it already recorded: same key, no second event


@pytest.mark.parametrize("scenario", ["schema_drift"], indirect=True)
async def test_a_breach_that_a_failing_pipeline_caused_names_the_incident_that_blocks_it(scenario: Container) -> None:
    blocker = (await scenario.collect_and_detect())[1][0]  # the failed sales pipeline: INC-2026-0042
    clock = scenario.clock
    clock.advance(timedelta(hours=1))  # type: ignore[attr-defined]
    _, opened = await scenario.sla_sweeper.sweep()
    sla_incident = next(i for i in opened if "sla-sales-gold-0630" in (i.correlation_id or ""))
    done, trace = await scenario.investigations.investigate(sla_incident.incident_id, actor=OPERATOR)
    assert trace.workflow == "sla-breach-v1" and done.category == "SLA_BREACH"
    root = done.probable_root_causes[0]
    assert blocker.incident_id in root.statement and "blocked the refresh" in root.statement
    assert done.confidence is not None and done.confidence.level is ConfidenceLevel.HIGH  # a known, open blocker
    dependency = next(f for f in done.confidence.factors if f.name == "dependency_correlation")
    assert dependency.value == 1.0  # the blocker is upstream of the freshness item
    assert_grounded(done)
    # the other breached SLA has no producer run and no incident: it simply did not run
    inventory = next(i for i in opened if i.incident_id != sla_incident.incident_id)
    quiet, _ = await scenario.investigations.investigate(inventory.incident_id, actor=OPERATOR)
    assert "no completed run" in quiet.probable_root_causes[0].statement


@pytest.mark.parametrize("scenario", ["schema_drift"], indirect=True)
async def test_a_failed_run_nobody_has_an_incident_for_is_reported_as_such(scenario: Container) -> None:
    blocker = (await scenario.collect_and_detect())[1][0]
    blocker.status = IncidentStatus.RESOLVED
    await scenario.incidents.repository.save(blocker)
    scenario.clock.advance(timedelta(hours=1))  # type: ignore[attr-defined]
    _, opened = await scenario.sla_sweeper.sweep()
    sales = next(i for i in opened if "sales-gold" in (i.correlation_id or ""))
    done, _ = await scenario.investigations.investigate(sales.incident_id, actor=OPERATOR)
    assert (
        "last run" in done.probable_root_causes[0].statement
        and "no incident is open" in done.probable_root_causes[0].statement
    )


# --------------------------------------------------------------------------------- schema drift in the log
@pytest.mark.parametrize("scenario", ["schema_drift_delta"], indirect=True)
async def test_schema_drift_is_found_in_the_delta_log_and_the_report_names_the_columns(scenario: Container) -> None:
    incident = await detect_and_investigate(scenario)
    assert incident.category == "SCHEMA_DRIFT" and incident.matched_runbooks[0].runbook_id == "RB-SCHEMA-DRIFT-001"
    assert incident.confidence is not None and incident.confidence.level is ConfidenceLevel.HIGH
    change = next(e for e in incident.evidence if e.evidence_type is EvidenceType.SCHEMA_CHANGE)
    assert change.source == "orqentis.collectors.delta-log"  # read from the log, not handed over as an event
    assert change.structured_payload["removed_columns"] == ["customer_id"]
    assert change.structured_payload["added_columns"] == ["client_id"]
    assert (change.structured_payload["previous_version"], change.structured_payload["current_version"]) == (41, 42)
    root = incident.probable_root_causes[0].statement
    assert "customer_id" in root and "client_id" in root and "unknown" not in root  # the live run said 'unknown'
    report = await scenario.evidence_store.get_text(incident.report_uri or "")
    assert "customer_id" in report and "client_id" in report
    error = next(e for e in incident.evidence if e.evidence_type is EvidenceType.ERROR_MESSAGE)
    assert error.structured_payload["signature_match"]["entities"]["column"] == "customer_id"
    assert_grounded(incident)
