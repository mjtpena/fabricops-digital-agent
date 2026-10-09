"""CRITICAL: the agent attempting a mutation without approval MUST fail.

Every path from the agent to a Fabric mutation is exercised here; none may execute anything.
"""

from __future__ import annotations

import typing
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from orqentis.actions.gateway import ActionBlockedError
from orqentis.bootstrap import Container, build_container
from orqentis.domain.actions import Action, ActionType
from orqentis.domain.approvals import ApprovalRequest, ApprovalStatus
from orqentis.domain.incidents import Incident
from orqentis.domain.principals import AGENT_PRINCIPAL
from orqentis.fabric.ports import FabricClient
from orqentis.tools.models import ToolStatus
from orqentis.tools.registry import ToolContext
from tests.conftest import ADMIN, OPERATOR, detect_and_investigate, make_settings

pytestmark = pytest.mark.security


class SpyWriteClient:
    """Write capability that records every call it receives."""

    def __init__(self) -> None:
        self.calls: list[tuple[Action, ApprovalRequest]] = []

    async def execute(self, action: Action, *, approval: ApprovalRequest) -> dict[str, Any]:
        self.calls.append((action, approval))
        return {"executed": True}


@pytest.fixture
def spy() -> SpyWriteClient:
    return SpyWriteClient()


@pytest.fixture
async def armed(tmp_path: Path, spy: SpyWriteClient) -> AsyncIterator[Container]:
    """Write capability ENABLED with a spy: proves the approval boundary, not the disabled flag, blocks."""
    c = await build_container(make_settings(tmp_path, enable_write_actions=True), reset=True, write_client=spy)
    try:
        yield c
    finally:
        await c.aclose()


def agent_ctx(c: Container, incident: Incident) -> ToolContext:
    return ToolContext(
        principal=AGENT_PRINCIPAL, trace_id="sec", incident=incident, allowed_tools=c.policy.tool_allowlist
    )


async def test_agent_mutation_tool_returns_requires_approval_and_executes_nothing(
    armed: Container, spy: SpyWriteClient
) -> None:
    incident = await detect_and_investigate(armed)
    response = await armed.registry.invoke(
        "request_pipeline_rerun",
        {"pipeline_id": incident.primary_item_id, "reason": "retry now please"},
        agent_ctx(armed, incident),
    )
    assert response.status is ToolStatus.ACTION_REQUIRES_APPROVAL
    assert response.data["executed"] is False
    approval = await armed.approvals.get(response.data["approval_id"])
    assert approval.status is ApprovalStatus.PENDING
    assert spy.calls == []


async def test_agent_cannot_execute_pending_approval(armed: Container, spy: SpyWriteClient) -> None:
    incident = await detect_and_investigate(armed)
    with pytest.raises(ActionBlockedError) as blocked:
        await armed.gateway.execute(incident.approval_ids[0], principal=AGENT_PRINCIPAL)
    assert blocked.value.code == "PRINCIPAL_NOT_HUMAN"
    assert spy.calls == []


async def test_agent_cannot_execute_even_an_approved_action(armed: Container, spy: SpyWriteClient) -> None:
    incident = await detect_and_investigate(armed)
    await armed.approvals.decide(incident.approval_ids[1], principal=OPERATOR, approve=True)
    with pytest.raises(ActionBlockedError, match="human"):
        await armed.gateway.execute(incident.approval_ids[1], principal=AGENT_PRINCIPAL)
    assert spy.calls == []


async def test_human_cannot_execute_without_approval(armed: Container, spy: SpyWriteClient) -> None:
    incident = await detect_and_investigate(armed)
    with pytest.raises(ActionBlockedError) as blocked:
        await armed.gateway.execute(incident.approval_ids[0], principal=OPERATOR)
    assert blocked.value.code == "ACTION_REQUIRES_APPROVAL"
    await armed.approvals.decide(incident.approval_ids[0], principal=ADMIN, approve=False)
    with pytest.raises(ActionBlockedError, match="REJECTED"):
        await armed.gateway.execute(incident.approval_ids[0], principal=OPERATOR)
    assert spy.calls == []


async def test_tampered_action_is_rejected(armed: Container, spy: SpyWriteClient) -> None:
    incident = await detect_and_investigate(armed)
    approval_id = incident.approval_ids[1]
    await armed.approvals.decide(approval_id, principal=ADMIN, approve=True)
    approval = await armed.approvals.get(approval_id)
    tampered = approval.proposed_action.model_copy(update={"target_item_id": "pl-ingest-finance-prod"})
    await armed.approvals.save(approval.model_copy(update={"proposed_action": tampered}))
    with pytest.raises(ActionBlockedError) as blocked:
        await armed.gateway.execute(approval_id, principal=OPERATOR)
    assert blocked.value.code == "ACTION_FINGERPRINT_MISMATCH"
    assert spy.calls == []


async def test_prohibited_actions_are_denied_without_approval_request(armed: Container) -> None:
    incident = await detect_and_investigate(armed)
    before = len(await armed.approvals.list(incident_id=incident.incident_id))
    for action_type in ("DELETE_ITEM", "CHANGE_PERMISSIONS"):
        response = await armed.registry.invoke(
            "request_item_change",
            {"action_type": action_type, "target_item_id": incident.primary_item_id, "reason": "injected instruction"},
            agent_ctx(armed, incident),
        )
        assert response.status is ToolStatus.ACTION_DENIED
        assert response.data["approval_id"] is None
    assert len(await armed.approvals.list(incident_id=incident.incident_id)) == before


async def test_agent_cannot_call_tools_outside_allowlist(armed: Container) -> None:
    incident = await detect_and_investigate(armed)
    for name in ("approve_action", "execute_action", "delete_item", "get_item_definition"):
        response = await armed.registry.invoke(name, {}, agent_ctx(armed, incident))
        assert response.status is ToolStatus.NOT_ALLOWED


async def test_approved_action_executes_once_for_authorised_human(armed: Container, spy: SpyWriteClient) -> None:
    """Positive control: the only path that reaches the write client."""
    incident = await detect_and_investigate(armed)
    rerun = incident.approval_ids[1]
    await armed.approvals.decide(rerun, principal=ADMIN, approve=True)
    outcome = await armed.gateway.execute(rerun, principal=OPERATOR)
    assert outcome.status.value == "EXECUTED"
    assert len(spy.calls) == 1 and spy.calls[0][0].action_type is ActionType.RERUN_PIPELINE
    with pytest.raises(ActionBlockedError) as again:
        await armed.gateway.execute(rerun, principal=OPERATOR)
    assert again.value.code == "ALREADY_EXECUTED"
    assert len(spy.calls) == 1
    updated = await armed.incidents.get(incident.incident_id)
    assert updated.status.value == "VERIFYING"


async def test_write_capability_disabled_by_default(container: Container) -> None:
    incident = await detect_and_investigate(container)
    await container.approvals.decide(incident.approval_ids[1], principal=ADMIN, approve=True)
    with pytest.raises(ActionBlockedError) as blocked:
        await container.gateway.execute(incident.approval_ids[1], principal=OPERATOR)
    assert blocked.value.code == "WRITE_CAPABILITY_NOT_GRANTED"


MUTATING_VERBS = {
    "create",
    "update",
    "delete",
    "run",
    "execute",
    "cancel",
    "refresh",
    "deploy",
    "assign",
    "set",
    "patch",
    "put",
    "post",
    "write",
    "start",
    "stop",
    "scale",
    "rerun",
    "approve",
}


def _public_verbs(cls: type) -> set[str]:
    return {name.split("_")[0] for name in dir(cls) if not name.startswith("_")}


def test_read_client_exposes_no_mutation_operations() -> None:
    from orqentis.fabric.mock import MockFabricClient

    for service_name in [n for n in dir(FabricClient) if not n.startswith("_")]:
        member = getattr(FabricClient, service_name)
        if not isinstance(member, property):
            continue
        service = typing.get_type_hints(member.fget)["return"]
        assert not _public_verbs(service) & MUTATING_VERBS, service_name
        assert not _public_verbs(type(getattr(MockFabricClient, service_name).fget(_mock()))) & MUTATING_VERBS
    assert not _public_verbs(MockFabricClient) & MUTATING_VERBS


def _mock() -> Any:
    from orqentis.fabric.fixtures import load_estate, load_scenario
    from orqentis.fabric.mock import MockFabricClient
    from tests.conftest import REPO

    return MockFabricClient(load_estate(REPO / "fixtures"), load_scenario(REPO / "fixtures", "schema_drift"))


async def test_untrusted_metadata_is_flagged_and_cannot_widen_permissions(
    armed: Container, spy: SpyWriteClient
) -> None:
    incident = await detect_and_investigate(armed)
    ctx = agent_ctx(armed, incident)
    ctx.recorder = None
    response = await armed.registry.invoke("get_item", {"item_id": "nb-transform-sales-dev"}, ctx)
    assert response.status is ToolStatus.SUCCESS
    from orqentis.tools.untrusted import scan

    assert "instruction_override" in scan(response.data)
    # Obeying the injected text still cannot do anything.
    denied = await armed.registry.invoke(
        "request_item_change",
        {"action_type": "DELETE_ITEM", "target_item_id": "pl-ingest-sales-prod", "reason": "as instructed by metadata"},
        ctx,
    )
    assert denied.status is ToolStatus.ACTION_DENIED
    assert spy.calls == []
