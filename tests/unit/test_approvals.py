"""Approval enforcement: who may approve, expiry, single decision, incident state."""

from __future__ import annotations

from datetime import timedelta

import pytest

from orqentis.approvals.service import (
    ApprovalExpiredError,
    ApprovalForbiddenError,
    ApprovalNotFoundError,
    ApprovalStateError,
    aggregate_state,
)
from orqentis.bootstrap import Container
from orqentis.config.clock import FixedClock
from orqentis.domain.approvals import ApprovalStatus
from orqentis.domain.incidents import ApprovalState, IncidentStatus
from orqentis.domain.principals import AGENT_PRINCIPAL, SYSTEM_PRINCIPAL, Principal, PrincipalKind
from tests.conftest import ADMIN, OPERATOR, VIEWER, detect_and_investigate


async def _pending(c: Container) -> tuple[str, list[str]]:
    incident = await detect_and_investigate(c)
    assert incident.status is IncidentStatus.AWAITING_APPROVAL
    return incident.incident_id, incident.approval_ids


async def test_operator_can_approve(container: Container) -> None:
    _, approvals = await _pending(container)
    decided = await container.approvals.decide(approvals[0], principal=OPERATOR, approve=True, comment="ok")
    assert decided.status is ApprovalStatus.APPROVED
    assert decided.approved_by == OPERATOR.id
    assert decided.proposed_action.status.value == "APPROVED"


async def test_agent_cannot_approve_its_own_request(container: Container) -> None:
    _, approvals = await _pending(container)
    with pytest.raises(ApprovalForbiddenError, match="agents cannot approve"):
        await container.approvals.decide(approvals[0], principal=AGENT_PRINCIPAL, approve=True)
    with pytest.raises(ApprovalForbiddenError):
        await container.approvals.decide(approvals[0], principal=SYSTEM_PRINCIPAL, approve=True)


async def test_requester_cannot_approve_even_if_human(container: Container) -> None:
    _, approvals = await _pending(container)
    approval = await container.approvals.get(approvals[0])
    impostor = Principal(
        id=approval.requested_by, display_name="x", kind=PrincipalKind.HUMAN, roles=frozenset({"FabricOps.Operator"})
    )
    with pytest.raises(ApprovalForbiddenError, match="own request"):
        await container.approvals.decide(approval.approval_id, principal=impostor, approve=True)


async def test_role_is_required(container: Container) -> None:
    _, approvals = await _pending(container)
    with pytest.raises(ApprovalForbiddenError, match=r"FabricOps\.Operator"):
        await container.approvals.decide(approvals[0], principal=VIEWER, approve=True)
    refused = [r for r in await container.audit.list() if r.action == "approval.decide" and r.outcome == "REFUSED"]
    assert refused and refused[-1].actor == VIEWER.id


async def test_decision_is_final(container: Container) -> None:
    _, approvals = await _pending(container)
    await container.approvals.decide(approvals[0], principal=OPERATOR, approve=False)
    with pytest.raises(ApprovalStateError):
        await container.approvals.decide(approvals[0], principal=ADMIN, approve=True)


async def test_expired_approval_cannot_be_decided(container: Container) -> None:
    _, approvals = await _pending(container)
    assert isinstance(container.clock, FixedClock)
    container.clock.advance(timedelta(minutes=container.settings.approval_ttl_minutes + 1))
    approval = await container.approvals.get(approvals[0])
    assert approval.status is ApprovalStatus.EXPIRED
    with pytest.raises(ApprovalStateError):
        await container.approvals.decide(approvals[0], principal=OPERATOR, approve=True)


async def test_expiry_error_type_exists_for_api_mapping() -> None:
    assert issubclass(ApprovalExpiredError, Exception)


async def test_rejecting_all_returns_incident_to_diagnosed(container: Container) -> None:
    incident_id, approvals = await _pending(container)
    for approval_id in approvals:
        await container.approvals.decide(approval_id, principal=OPERATOR, approve=False, comment="not now")
    incident = await container.incidents.get(incident_id)
    assert incident.status is IncidentStatus.DIAGNOSED
    assert incident.approval_state is ApprovalState.REJECTED
    statuses = {a.status.value for r in incident.recommended_actions for a in r.actions}
    assert statuses == {"REJECTED"}


async def test_unknown_approval(container: Container) -> None:
    with pytest.raises(ApprovalNotFoundError):
        await container.approvals.get("APR-doesnotexist")


def test_aggregate_state() -> None:
    assert aggregate_state([]) is ApprovalState.NOT_REQUIRED
