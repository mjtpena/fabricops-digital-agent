"""Approval decisions and execution are atomic: one decision wins, one execution is claimed.

These tests race real coroutines against the SQLite provider. They also race two service/gateway instances that
share only the provider (no common in-process lock), which is what separate processes or replicas look like:
there the compare-and-set on ``ApprovalProvider.transition`` is the only thing that can pick a single winner.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Callable
from datetime import timedelta
from importlib import resources
from pathlib import Path
from typing import Any

import pytest

from orqentis.actions.gateway import ActionBlockedError, ActionGateway, ActionOutcome, GatewayStatus
from orqentis.approvals.service import ApprovalService, ApprovalStateError
from orqentis.bootstrap import Container, build_container
from orqentis.config.clock import FixedClock
from orqentis.domain.actions import Action, ActionStatus
from orqentis.domain.approvals import ApprovalRequest, ApprovalStatus
from orqentis.domain.principals import AGENT_PRINCIPAL, Principal, PrincipalKind
from orqentis.fabric.errors import FabricError
from orqentis.policies.engine import PolicyEngine
from tests.conftest import ADMIN, OPERATOR, detect_and_investigate, make_settings

pytestmark = pytest.mark.security


class SlowSpy:
    """Write capability that yields to the event loop inside the call, so racing executes interleave."""

    def __init__(self, failure: Exception | None = None) -> None:
        self.calls: list[tuple[Action, ApprovalRequest]] = []
        self._failure = failure

    async def execute(self, action: Action, *, approval: ApprovalRequest) -> dict[str, Any]:
        self.calls.append((action, approval))
        await asyncio.sleep(0.02)
        if self._failure is not None:
            raise self._failure
        return {"executed": True}


@pytest.fixture
def spy() -> SlowSpy:
    return SlowSpy()


@pytest.fixture
async def armed(tmp_path: Path, spy: SlowSpy) -> AsyncIterator[Container]:
    c = await build_container(make_settings(tmp_path, enable_write_actions=True), reset=True, write_client=spy)
    try:
        yield c
    finally:
        await c.aclose()


def second_process(c: Container, write_client: SlowSpy, policy: PolicyEngine | None = None) -> ActionGateway:
    """A service and gateway that share only the provider and storage with ``c``: no common lock."""
    approvals = ApprovalService(c.approvals.provider, c.incidents, c.audit, c.clock, ttl=timedelta(minutes=240))
    return ActionGateway(policy or c.policy, approvals, write_client, c.incidents, c.audit, c.clock, write_enabled=True)


async def approved(c: Container, index: int = 0) -> str:
    incident = await detect_and_investigate(c)
    approval_id = incident.approval_ids[index]
    await c.approvals.decide(approval_id, principal=ADMIN, approve=True)
    return approval_id


def outcomes(results: list[Any]) -> tuple[list[ActionOutcome], list[ActionBlockedError], list[BaseException]]:
    done = [r for r in results if isinstance(r, ActionOutcome)]
    blocked = [r for r in results if isinstance(r, ActionBlockedError)]
    other = [r for r in results if isinstance(r, BaseException) and not isinstance(r, ActionBlockedError)]
    return done, blocked, other


# ---- single use -------------------------------------------------------------------------------------------


async def test_concurrent_executes_reach_the_write_client_exactly_once(armed: Container, spy: SlowSpy) -> None:
    approval_id = await approved(armed)
    results = await asyncio.gather(
        *[armed.gateway.execute(approval_id, principal=ADMIN) for _ in range(5)], return_exceptions=True
    )
    done, blocked, other = outcomes(list(results))
    assert other == []
    assert len(done) == 1 and done[0].status is GatewayStatus.EXECUTED
    assert [b.code for b in blocked] == ["ALREADY_EXECUTED"] * 4
    assert len(spy.calls) == 1
    stored = await armed.approvals.get(approval_id)
    assert stored.proposed_action.status is ActionStatus.SUCCEEDED


async def test_executes_in_separate_processes_reach_the_write_client_exactly_once(
    armed: Container, spy: SlowSpy
) -> None:
    approval_id = await approved(armed)
    other_gateway = second_process(armed, spy)
    results = await asyncio.gather(
        *[g.execute(approval_id, principal=ADMIN) for g in (armed.gateway, other_gateway) * 3], return_exceptions=True
    )
    done, blocked, other = outcomes(list(results))
    assert other == []
    assert len(done) == 1 and len(blocked) == 5
    assert {b.code for b in blocked} == {"ALREADY_EXECUTED"}
    assert len(spy.calls) == 1


async def test_the_claim_is_persisted_before_the_write_call(armed: Container) -> None:
    approval_id = await approved(armed)
    seen: list[ActionStatus] = []

    class Probe:
        async def execute(self, action: Action, *, approval: ApprovalRequest) -> dict[str, Any]:
            stored = await armed.approvals.get(approval_id)
            seen.append(stored.proposed_action.status)
            return {"executed": True}

    gateway = ActionGateway(
        armed.policy, armed.approvals, Probe(), armed.incidents, armed.audit, armed.clock, write_enabled=True
    )
    await gateway.execute(approval_id, principal=ADMIN)
    assert seen == [ActionStatus.EXECUTING]


async def test_an_unknown_outcome_keeps_the_claim_so_a_retry_cannot_repeat_the_write(
    tmp_path: Path,
) -> None:
    broken = SlowSpy(failure=RuntimeError("connection reset after sending the request"))
    c = await build_container(make_settings(tmp_path, enable_write_actions=True), reset=True, write_client=broken)
    try:
        approval_id = await approved(c)
        with pytest.raises(RuntimeError):
            await c.gateway.execute(approval_id, principal=ADMIN)
        stored = await c.approvals.get(approval_id)
        assert stored.proposed_action.status is ActionStatus.EXECUTING
        with pytest.raises(ActionBlockedError) as retry:
            await c.gateway.execute(approval_id, principal=ADMIN)
        assert retry.value.code == "ALREADY_EXECUTED"
        assert len(broken.calls) == 1
        audit = await c.audit.list(incident_id=stored.incident_id)
        assert any(r.action == "action.execute" and r.outcome == "UNKNOWN" for r in audit)
    finally:
        await c.aclose()


async def test_a_failed_write_is_final_too(tmp_path: Path) -> None:
    failing = SlowSpy(failure=FabricError("workspace not found"))
    c = await build_container(make_settings(tmp_path, enable_write_actions=True), reset=True, write_client=failing)
    try:
        approval_id = await approved(c)
        outcome = await c.gateway.execute(approval_id, principal=ADMIN)
        assert outcome.status is GatewayStatus.EXECUTION_FAILED
        with pytest.raises(ActionBlockedError) as retry:
            await c.gateway.execute(approval_id, principal=ADMIN)
        assert retry.value.code == "ALREADY_EXECUTED" and len(failing.calls) == 1
    finally:
        await c.aclose()


# ---- decisions --------------------------------------------------------------------------------------------


async def test_concurrent_approve_and_reject_exactly_one_decision_wins(armed: Container) -> None:
    incident = await detect_and_investigate(armed)
    approval_id = incident.approval_ids[0]
    results = await asyncio.gather(
        armed.approvals.decide(approval_id, principal=OPERATOR, approve=True),
        armed.approvals.decide(approval_id, principal=ADMIN, approve=False),
        return_exceptions=True,
    )
    winners = [r for r in results if isinstance(r, ApprovalRequest)]
    losers = [r for r in results if isinstance(r, ApprovalStateError)]
    assert len(winners) == 1 and len(losers) == 1
    stored = await armed.approvals.get(approval_id)
    assert stored.status is winners[0].status
    assert stored.status in {ApprovalStatus.APPROVED, ApprovalStatus.REJECTED}


async def test_decisions_in_separate_processes_exactly_one_wins(armed: Container, spy: SlowSpy) -> None:
    incident = await detect_and_investigate(armed)
    approval_id = incident.approval_ids[0]
    other = ApprovalService(armed.approvals.provider, armed.incidents, armed.audit, armed.clock, ttl=timedelta(hours=4))
    humans = [
        Principal(id=f"approver-{i}@contoso.com", display_name="A", kind=PrincipalKind.HUMAN, roles=ADMIN.roles)
        for i in range(6)
    ]
    services = [armed.approvals, other] * 3
    results = await asyncio.gather(
        *[
            svc.decide(approval_id, principal=human, approve=index % 2 == 0)
            for index, (svc, human) in enumerate(zip(services, humans, strict=True))
        ],
        return_exceptions=True,
    )
    assert sum(isinstance(r, ApprovalRequest) for r in results) == 1
    assert sum(isinstance(r, ApprovalStateError) for r in results) == 5
    stored = await armed.approvals.get(approval_id)
    winner = next(r for r in results if isinstance(r, ApprovalRequest))
    assert stored.status is winner.status and stored.revision == winner.revision


async def test_the_provider_refuses_a_stale_transition(armed: Container) -> None:
    incident = await detect_and_investigate(armed)
    approval = await armed.approvals.get(incident.approval_ids[0])
    provider = armed.approvals.provider
    first = await provider.transition(
        approval.model_copy(update={"status": ApprovalStatus.CANCELLED}), expected_revision=approval.revision
    )
    assert first is not None and first.revision == approval.revision + 1
    stale = await provider.transition(
        approval.model_copy(update={"status": ApprovalStatus.APPROVED}), expected_revision=approval.revision
    )
    assert stale is None
    assert (await armed.approvals.get(approval.approval_id)).status is ApprovalStatus.CANCELLED


async def test_expiry_cannot_overwrite_a_decision_made_in_the_meantime(armed: Container) -> None:
    incident = await detect_and_investigate(armed)
    approval = await armed.approvals.get(incident.approval_ids[0])
    await armed.approvals.decide(approval.approval_id, principal=ADMIN, approve=True)
    # A reader holding the stale PENDING copy tries to expire it after its deadline.
    late = approval.model_copy(update={"expires_at": armed.clock.now() - timedelta(minutes=1)})
    result = await armed.approvals._expire_if_due(late)
    assert result.status is ApprovalStatus.APPROVED
    assert (await armed.approvals.get(approval.approval_id)).status is ApprovalStatus.APPROVED


# ---- the current policy is enforced at execution time ------------------------------------------------------


def policy_from(tmp_path: Path, c: Container, edit: Callable[[str], str]) -> PolicyEngine:
    text = resources.files("orqentis.policies").joinpath("default_policy.yaml").read_text(encoding="utf-8")
    edited = edit(text)
    assert edited != text, "the edit changed nothing"
    path = tmp_path / "edited_policy.yaml"
    path.write_text(edited, encoding="utf-8")
    return PolicyEngine.from_file(path, c.clock)


async def test_a_policy_tightened_after_approval_is_enforced_at_execution(
    armed: Container, spy: SlowSpy, tmp_path: Path
) -> None:
    approval_id = await approved(armed)
    tightened = policy_from(
        tmp_path,
        armed,
        lambda t: t.replace("required_role: FabricOps.Operator", "required_role: FabricOps.PlatformAdmin"),
    )
    gateway = second_process(armed, spy, tightened)
    with pytest.raises(ActionBlockedError) as blocked:
        await gateway.execute(approval_id, principal=OPERATOR)
    assert blocked.value.code == "ROLE_REQUIRED" and "current policy" in blocked.value.message
    assert spy.calls == []
    # A person who holds the now-required role may still execute it.
    outcome = await gateway.execute(approval_id, principal=ADMIN)
    assert outcome.status is GatewayStatus.EXECUTED and len(spy.calls) == 1


async def test_an_action_reclassified_to_a_higher_risk_needs_a_new_approval(
    armed: Container, spy: SlowSpy, tmp_path: Path
) -> None:
    approval_id = await approved(armed)
    action_type = (await armed.approvals.get(approval_id)).proposed_action.action_type.value
    escalated = policy_from(
        tmp_path,
        armed,
        lambda t: re.sub(rf"(?m)^(  {action_type}: )\w+", lambda m: f"{m.group(1)}HIGH_RISK", t),
    )
    gateway = second_process(armed, spy, escalated)
    with pytest.raises(ActionBlockedError) as blocked:
        await gateway.execute(approval_id, principal=ADMIN)
    assert blocked.value.code == "POLICY_RISK_ESCALATED"
    assert spy.calls == []
    assert (await armed.approvals.get(approval_id)).proposed_action.status is ActionStatus.APPROVED


# ---- one open approval per action -------------------------------------------------------------------------


async def asked_again(c: Container, approval_id: str) -> ActionOutcome:
    """The agent (or a second investigation) proposes the very action that approval ``approval_id`` is for."""
    approval = await c.approvals.get(approval_id)
    incident = await c.incidents.get(approval.incident_id)
    again = approval.proposed_action.model_copy(update={"status": ActionStatus.PROPOSED, "approval_id": None})
    return await c.gateway.submit(again, principal=AGENT_PRINCIPAL, incident=incident)


async def test_proposing_an_action_that_is_already_granted_and_unused_returns_that_approval(
    armed: Container, spy: SlowSpy
) -> None:
    """Reproduced: it opened a second approval; both could be granted and executed, and the write happened twice."""
    approval_id = await approved(armed)
    outcome = await asked_again(armed, approval_id)
    assert outcome.status is GatewayStatus.ACTION_REQUIRES_APPROVAL
    assert outcome.approval is not None and outcome.approval.approval_id == approval_id
    assert outcome.action.approval_id == approval_id and outcome.action.status is ActionStatus.APPROVED
    assert "already granted" in outcome.message
    approval = await armed.approvals.get(approval_id)
    same_action = [
        a
        for a in await armed.approvals.list(incident_id=approval.incident_id)
        if a.action_fingerprint == approval.action_fingerprint
    ]
    assert [a.approval_id for a in same_action] == [approval_id]


async def test_once_the_granted_action_has_been_used_it_can_be_requested_again(armed: Container, spy: SlowSpy) -> None:
    approval_id = await approved(armed)
    assert (await armed.gateway.execute(approval_id, principal=ADMIN)).status is GatewayStatus.EXECUTED
    outcome = await asked_again(armed, approval_id)
    assert outcome.approval is not None and outcome.approval.approval_id != approval_id
    assert outcome.approval.status is ApprovalStatus.PENDING


async def test_a_granted_approval_that_ran_out_unused_is_ended_when_the_action_is_requested_again(
    armed: Container, spy: SlowSpy
) -> None:
    approval_id = await approved(armed)
    assert isinstance(armed.clock, FixedClock)
    armed.clock.advance(timedelta(minutes=armed.settings.approval_ttl_minutes + 1))
    outcome = await asked_again(armed, approval_id)
    assert outcome.approval is not None and outcome.approval.approval_id != approval_id
    assert outcome.approval.status is ApprovalStatus.PENDING
    assert (await armed.approvals.get(approval_id)).status is ApprovalStatus.EXPIRED
    expiries = [r for r in await armed.audit.list() if r.action == "approval.expire"]
    assert any(r.details.get("approval_id") == approval_id for r in expiries)
