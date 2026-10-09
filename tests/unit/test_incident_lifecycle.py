"""Incident model invariants and lifecycle transitions."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from orqentis.domain.actions import Action, ActionType
from orqentis.domain.common import Environment
from orqentis.domain.evidence import Finding, FindingCategory
from orqentis.domain.incidents import ALLOWED_TRANSITIONS, Incident, IncidentStatus
from orqentis.domain.taxonomy import Severity

NOW = datetime(2026, 10, 5, tzinfo=UTC)


def incident(status: IncidentStatus = IncidentStatus.DETECTED) -> Incident:
    return Incident(
        incident_id="INC-1",
        tenant_id="t",
        title="x",
        status=status,
        severity=Severity.HIGH,
        category="PIPELINE_FAILURE",
        detected_at=NOW,
        source="s",
        workspace_id="w",
        primary_item_id="p",
        created_at=NOW,
        updated_at=NOW,
    )


def test_finding_requires_evidence() -> None:
    with pytest.raises(ValidationError):
        Finding(
            finding_id="F1",
            incident_id="INC-1",
            statement="x",
            category=FindingCategory.ROOT_CAUSE,
            confidence=0.5,
            evidence_ids=[],
            reasoning_summary="",
            produced_by="t",
            created_at=NOW,
        )


def test_incident_rejects_unknown_category() -> None:
    with pytest.raises(ValidationError):
        Incident(**{**incident().model_dump(), "category": "MADE_UP"})


@pytest.mark.parametrize(
    ("current", "target", "allowed"),
    [
        (IncidentStatus.DETECTED, IncidentStatus.INVESTIGATING, True),
        (IncidentStatus.DETECTED, IncidentStatus.REMEDIATING, False),
        (IncidentStatus.DIAGNOSED, IncidentStatus.AWAITING_APPROVAL, True),
        (IncidentStatus.AWAITING_APPROVAL, IncidentStatus.REMEDIATING, True),
        (IncidentStatus.DIAGNOSED, IncidentStatus.REMEDIATING, False),  # no remediation without approval
        (IncidentStatus.REMEDIATING, IncidentStatus.VERIFYING, True),
        (IncidentStatus.VERIFYING, IncidentStatus.RESOLVED, True),
        (IncidentStatus.CLOSED, IncidentStatus.INVESTIGATING, False),
    ],
)
def test_transitions(current: IncidentStatus, target: IncidentStatus, allowed: bool) -> None:
    assert incident(current).can_transition_to(target) is allowed


def test_every_status_has_transition_rules() -> None:
    assert set(ALLOWED_TRANSITIONS) == set(IncidentStatus)
    assert IncidentStatus.REMEDIATING in ALLOWED_TRANSITIONS[IncidentStatus.AWAITING_APPROVAL]
    assert all(
        IncidentStatus.REMEDIATING not in targets
        for status, targets in ALLOWED_TRANSITIONS.items()
        if status is not IncidentStatus.AWAITING_APPROVAL
    )


def test_action_fingerprint_binds_what_is_executed() -> None:
    base = Action(
        action_id="A1",
        incident_id="INC-1",
        action_type=ActionType.RERUN_PIPELINE,
        title="t",
        target_item_id="pl",
        environment=Environment.PROD,
        requested_by="r",
        created_at=NOW,
    )
    same_but_cosmetic = base.model_copy(update={"action_id": "A2", "title": "other", "description": "x"})
    other_target = base.model_copy(update={"target_item_id": "pl-other"})
    other_params = base.model_copy(update={"parameters": {"force": True}})
    assert base.fingerprint() == same_but_cosmetic.fingerprint()
    assert base.fingerprint() != other_target.fingerprint()
    assert base.fingerprint() != other_params.fingerprint()
