"""End-to-end synthetic incident scenarios (telemetry -> detection -> investigation -> report)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from orqentis.bootstrap import Container, build_container
from orqentis.domain.confidence import ConfidenceLevel
from orqentis.domain.evidence import EvidenceType, FindingCategory
from orqentis.domain.incidents import ApprovalState, IncidentStatus
from orqentis.domain.sla import SLAState
from tests.conftest import detect_and_investigate, make_settings

pytestmark = pytest.mark.scenario


@pytest.fixture
async def scenario(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[Container]:
    c = await build_container(make_settings(tmp_path, scenario=request.param), reset=True)
    try:
        yield c
    finally:
        await c.aclose()


def findings(incident: object, category: FindingCategory) -> list[str]:
    return [f.statement for f in incident.findings if f.category is category]  # type: ignore[attr-defined]


@pytest.mark.parametrize("scenario", ["schema_drift"], indirect=True)
async def test_pipeline_schema_drift(scenario: Container) -> None:
    incident = await detect_and_investigate(scenario)
    assert incident.status is IncidentStatus.AWAITING_APPROVAL
    assert incident.category == "SCHEMA_DRIFT"
    root = incident.probable_root_causes[0]
    assert "customer_id" in root.statement and "SalesBronze" in root.statement and "client_id" in root.statement
    evidence = incident.evidence_by_id()
    assert {evidence[i].evidence_type for i in root.evidence_ids} >= {
        EvidenceType.ERROR_MESSAGE,
        EvidenceType.SCHEMA_CHANGE,
    }
    assert incident.confidence is not None and incident.confidence.level is ConfidenceLevel.HIGH
    # Blast radius: the spec's downstream assets, plus cross-workspace finance consumers.
    names = {a.display_name for a in incident.affected_assets}
    assert {"sales_gold", "SalesSemanticModel", "ExecutiveSalesReport", "FinanceSemanticModel"} <= names
    assert incident.blast_radius is not None and incident.blast_radius.cross_workspace
    assert [(s.data_product_name, s.state) for s in incident.sla_impact] == [("sales_gold", SLAState.AT_RISK)]
    assert [m.incident_id for m in incident.related_incidents][:1] == ["INC-2026-0041"]
    assert incident.matched_runbooks[0].runbook_id == "RB-SCHEMA-DRIFT-001"
    recommendation = incident.recommended_actions[0]
    assert recommendation.runbook_id == "RB-SCHEMA-DRIFT-001"
    assert recommendation.risk.value == "MEDIUM_RISK" and recommendation.approval_required
    assert {a.action_type.value for a in recommendation.actions} == {"MODIFY_ITEM_DEFINITION", "RERUN_PIPELINE"}
    assert incident.approval_state is ApprovalState.PENDING and len(incident.approval_ids) == 2
    assert findings(incident, FindingCategory.CONFIGURATION_DRIFT)  # code change ruled out
    assert any("ruled out" in s for s in findings(incident, FindingCategory.CAPACITY_PRESSURE))
    # Every finding cites evidence that exists on the incident.
    for finding in incident.findings:
        assert finding.evidence_ids and all(e in evidence for e in finding.evidence_ids)
    # Report and trace persisted next to the evidence.
    artefacts = await scenario.evidence_store.list(incident.incident_id)
    assert f"evidence://{incident.incident_id}/report.md" in artefacts
    assert any(a.endswith(".json") and "/trace-" in a for a in artefacts)
    report = await scenario.evidence_store.get_text(incident.report_uri or "")
    for heading in (
        "Executive Summary",
        "Probable Root Cause",
        "Blast Radius",
        "SLA Impact",
        "Matched Runbook",
        "Approval Requirement",
        "Validation Plan",
        "Escalation",
    ):
        assert f"## {heading}" in report
    for label in ("**[FACT]**", "**[INFERENCE]**", "**[RECOMMENDATION]**"):
        assert label in report
    assert (await scenario.audit.verify()).valid


@pytest.mark.parametrize("scenario", ["auth_failure"], indirect=True)
async def test_authentication_failure(scenario: Container) -> None:
    incident = await detect_and_investigate(scenario)
    assert incident.category == "AUTH_FAILURE"
    root = incident.probable_root_causes[0]
    assert "app-fabric-erp-reader" in root.statement and "expired" in root.statement
    assert incident.confidence is not None and incident.confidence.level is ConfidenceLevel.HIGH
    assert incident.matched_runbooks[0].runbook_id == "RB-AUTH-FAILURE-001"
    actions = {a.action_type.value: a for a in incident.recommended_actions[0].actions}
    assert actions["UPDATE_CONNECTION_CREDENTIAL"].target_item_id == "src-erp-gl"
    assert [m.incident_id for m in incident.related_incidents][:1] == ["INC-2026-0012"]
    # The secret embedded in the Fabric error message never reaches evidence or the report.
    serialized = incident.model_dump_json()
    assert "Zx9~sQ.8hY2" not in serialized
    assert "Zx9~sQ.8hY2" not in await scenario.evidence_store.get_text(incident.report_uri or "")
    assert [(s.data_product_name, s.state) for s in incident.sla_impact] == [("gl_daily_balances", SLAState.AT_RISK)]


@pytest.mark.parametrize("scenario", ["capacity_pressure"], indirect=True)
async def test_capacity_pressure(scenario: Container) -> None:
    incident = await detect_and_investigate(scenario)
    assert incident.category == "CAPACITY_PRESSURE"
    assert "117% CU" in incident.probable_root_causes[0].statement
    contention = findings(incident, FindingCategory.CAPACITY_PRESSURE)
    assert any("ml_feature_backfill" in s and "Data Science Sandbox" in s for s in contention)
    pattern = findings(incident, FindingCategory.HISTORICAL_PATTERN)
    assert any("3 consecutive Mondays" in s and "ml_feature_backfill" in s for s in pattern)
    assert incident.matched_runbooks[0].runbook_id == "RB-CAPACITY-THROTTLING-001"
    targets = {a.action_type.value: a.target_item_id for a in incident.recommended_actions[0].actions}
    assert targets == {"RERUN_PIPELINE": "pl-ingest-inventory-prod", "MODIFY_CONFIGURATION": "nb-ml-feature-backfill"}
    assert incident.confidence is not None and incident.confidence.level is ConfidenceLevel.HIGH
    dependency = next(f for f in incident.confidence.factors if f.name == "dependency_correlation")
    assert dependency.value == 0.5  # shared capacity, not lineage


@pytest.mark.parametrize("scenario", ["unknown_failure"], indirect=True)
async def test_unknown_failure(scenario: Container) -> None:
    incident = await detect_and_investigate(scenario)
    assert incident.category == "UNKNOWN"
    assert incident.status is IncidentStatus.DIAGNOSED
    assert incident.probable_root_causes == []
    assert incident.insufficient_evidence_reason and "cannot be determined" in incident.insufficient_evidence_reason
    assert incident.confidence is not None and incident.confidence.level is ConfidenceLevel.LOW
    assert incident.matched_runbooks == []
    recommendation = incident.recommended_actions[0]
    assert recommendation.risk.value == "READ_ONLY" and not recommendation.approval_required
    assert incident.approval_state is ApprovalState.NOT_REQUIRED and incident.approval_ids == []
    # The recent deployment is surfaced as a possible factor, not asserted as the cause.
    contributing = findings(incident, FindingCategory.CONTRIBUTING_FACTOR)
    assert any("deployed to production" in s and "no evidence links it" in s for s in contributing)


@pytest.mark.parametrize("scenario", ["schema_drift"], indirect=True)
async def test_reinvestigation_supersedes_pending_approvals(scenario: Container) -> None:
    from tests.conftest import OPERATOR

    first = await detect_and_investigate(scenario)
    second, _ = await scenario.investigations.investigate(first.incident_id, actor=OPERATOR)
    old = [await scenario.approvals.get(a) for a in first.approval_ids]
    assert {a.status.value for a in old} == {"CANCELLED"}
    assert second.status is IncidentStatus.AWAITING_APPROVAL
    assert (
        len([a for a in await scenario.approvals.list(incident_id=first.incident_id) if a.status.value == "PENDING"])
        == 2
    )
