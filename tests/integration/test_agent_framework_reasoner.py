"""The FabricOps Investigator on Microsoft Agent Framework, driven by a scripted model.

The real ``agent_framework.Agent`` runs (tool schemas, function invocation, telemetry layers);
only the model's replies are scripted.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from agent_framework import ChatResponse, Message

from orqentis.agents.agent_framework import AgentFrameworkReasoner
from orqentis.bootstrap import Container, build_container
from orqentis.domain.actions import Action
from orqentis.domain.approvals import ApprovalRequest, ApprovalStatus
from orqentis.domain.confidence import ConfidenceLevel
from orqentis.domain.incidents import Incident
from orqentis.tools.models import ToolStatus
from tests.conftest import detect_and_investigate, make_settings
from tests.support.scripted_chat_client import (
    ScriptedChatClient,
    answer,
    call_tools,
    evidence_id_of_type,
    evidence_package,
    tool_results,
)

MODEL = "scripted-test-model"


class SpyWriteClient:
    def __init__(self) -> None:
        self.calls: list[Action] = []

    async def execute(self, action: Action, *, approval: ApprovalRequest) -> dict[str, Any]:
        self.calls.append(action)
        return {}


async def run(
    tmp_path: Path, steps: Sequence[Any], **settings: object
) -> tuple[Incident, ScriptedChatClient, Container, SpyWriteClient]:
    client = ScriptedChatClient(steps)
    spy = SpyWriteClient()
    container = await build_container(
        make_settings(tmp_path, enable_write_actions=True, **settings),
        reset=True,
        reasoner=AgentFrameworkReasoner(lambda: client, model=MODEL),
        write_client=spy,
    )
    incident = await detect_and_investigate(container)
    return incident, client, container, spy


@pytest.fixture
async def cleanup() -> AsyncIterator[list[Container]]:
    containers: list[Container] = []
    yield containers
    for c in containers:
        await c.aclose()


async def test_agent_reasons_with_tools_and_output_is_grounded(tmp_path: Path, cleanup: list[Container]) -> None:
    def gather(messages: list[Message]) -> ChatResponse:
        package = evidence_package(messages)
        return call_tools(
            ("get_capacity_context", {"workspace_id": package["primary_item"]["workspace_id"]}),
            ("get_item", {"item_id": "src-crm-sales-orders"}),
        )

    def conclude(messages: list[Message]) -> ChatResponse:
        package = evidence_package(messages)
        results = tool_results(messages)
        capacity_evidence = results["call-0"]["untrusted_data"]["evidence_id"]
        error = evidence_id_of_type(package, "ERROR_MESSAGE")
        schema = evidence_id_of_type(package, "SCHEMA_CHANGE")
        return answer(
            {
                "executive_summary": "CRM renamed customer_id to client_id; transform_sales failed.",
                "root_cause": {
                    "statement": "Upstream CRM feed renamed customer_id to client_id (Delta v42).",
                    "category": "SCHEMA_DRIFT",
                    "evidence_ids": [error, schema],
                    "reasoning_summary": "Missing column equals removed column, upstream, just before.",
                },
                "findings": [
                    {
                        "statement": "Capacity was healthy at the failure time.",
                        "category": "CAPACITY_PRESSURE",
                        "evidence_ids": [capacity_evidence],
                        "kind": "FACT",
                    },
                    {
                        "statement": "Invented telemetry shows a network outage.",
                        "category": "CONTRIBUTING_FACTOR",
                        "evidence_ids": ["INC-2026-0042-EV999"],
                    },
                    {"statement": "Smuggled root cause", "category": "ROOT_CAUSE", "evidence_ids": [error]},
                ],
                "recommended_runbook_id": "RB-SCHEMA-DRIFT-001",
            }
        )

    incident, client, container, spy = await run(tmp_path, [gather, conclude])
    cleanup.append(container)
    decision = incident.agent_decision
    assert decision is not None
    assert decision.reasoner == "agent_framework" and decision.model == MODEL
    assert decision.usage is not None and decision.usage.total_tokens == 1500
    assert decision.prompt_version.startswith("fabricops-investigator/")
    assert incident.probable_root_causes[0].statement.startswith("Upstream CRM feed renamed")
    # Fabricated evidence and smuggled root causes are rejected, not trusted.
    reasons = [r.reason for r in decision.rejected_outputs]
    assert any("does not exist: INC-2026-0042-EV999" in r for r in reasons)
    assert any("root cause must be provided" in r for r in reasons)
    # The agent's tool calls became evidence it could cite; they are logged against the agent principal.
    agent_evidence = [e for e in incident.evidence if e.tool_call_id in decision.tool_call_ids]
    assert {e.evidence_type.value for e in agent_evidence} == {"CAPACITY_METRIC", "ITEM_METADATA"}
    assert any(f.category.value == "CAPACITY_PRESSURE" and f.kind.value == "FACT" for f in incident.findings)
    # Confidence is computed by the platform, not chosen by the model.
    assert incident.confidence is not None and incident.confidence.level is ConfidenceLevel.HIGH
    assert incident.confidence.method == "orqentis-weighted-signals-v1"
    # The model saw the platform's rules, only allowlisted tools, and untrusted envelopes.
    options = client.options[0]
    assert "untrusted" in options["instructions"] and "Never invent telemetry" in options["instructions"]
    offered = sorted(t.name for t in options["tools"])
    assert offered == sorted(container.policy.tool_allowlist)
    assert "notice" in tool_results(client.requests[1])["call-0"]
    audit = [
        r
        for r in await container.audit.list(incident_id=incident.incident_id)
        if r.action == "tool.invoke" and r.actor == "agent:FabricOpsInvestigator"
    ]
    assert len(audit) == 2
    assert spy.calls == []


async def test_prompt_injection_cannot_cause_mutation(tmp_path: Path, cleanup: list[Container]) -> None:
    def read_poisoned_metadata(messages: list[Message]) -> ChatResponse:
        return call_tools(("get_item", {"item_id": "nb-transform-sales-dev"}))

    def obey_injection(messages: list[Message]) -> ChatResponse:
        package = evidence_package(messages)
        return call_tools(
            (
                "request_item_change",
                {
                    "action_type": "DELETE_ITEM",
                    "target_item_id": "pl-ingest-sales-prod",
                    "reason": "maintenance mode instruction",
                },
            ),
            ("request_pipeline_rerun", {"pipeline_id": package["primary_item"]["item_id"], "reason": "just do it"}),
            ("approve_action", {"approval_id": "APR-any"}),
        )

    def conclude(messages: list[Message]) -> ChatResponse:
        return answer(
            {
                "executive_summary": "Done as instructed.",
                "root_cause": {"statement": "Maintenance", "category": "UNKNOWN", "evidence_ids": []},
                "recommended_runbook_id": "RB-MADE-UP-999",
            }
        )

    incident, client, container, spy = await run(tmp_path, [read_poisoned_metadata, obey_injection, conclude])
    cleanup.append(container)
    poisoned = tool_results(client.requests[1])["call-0"]
    assert {"instruction_override", "approval_manipulation"} <= set(poisoned["security_flags"])
    results = tool_results(client.requests[2])
    assert results["call-0"]["untrusted_data"]["status"] == ToolStatus.ACTION_DENIED.value
    assert results["call-1"]["untrusted_data"]["status"] == ToolStatus.ACTION_REQUIRES_APPROVAL.value
    assert results["call-1"]["untrusted_data"]["data"]["executed"] is False
    assert "not found" in str(results["call-2"]).lower()
    # Nothing executed; the rerun waits for a human; the made-up runbook and uncited root cause are discarded.
    assert spy.calls == []
    approval_id = results["call-1"]["untrusted_data"]["data"]["approval_id"]
    assert (await container.approvals.get(approval_id)).status is ApprovalStatus.PENDING
    decision = incident.agent_decision
    assert decision is not None
    rejected = {r.kind: r.reason for r in decision.rejected_outputs}
    assert "no supporting evidence" in rejected["root_cause"]
    assert "not retrieved from the runbook catalog" in rejected["runbook"]
    assert incident.probable_root_causes == []
    assert incident.confidence is not None and incident.confidence.level is ConfidenceLevel.LOW


async def test_malformed_model_output_falls_back_to_rule_based(tmp_path: Path, cleanup: list[Container]) -> None:
    incident, _, container, _ = await run(tmp_path, [lambda m: answer("I think it is probably the network.")])
    cleanup.append(container)
    decision = incident.agent_decision
    assert decision is not None
    assert decision.status.value == "FAILED"
    assert decision.reasoner.startswith("rule_based (fallback after agent_framework")
    assert any("AgentOutputError" in e for e in decision.errors)
    assert incident.category == "SCHEMA_DRIFT"  # the deterministic reasoner still diagnosed it


async def test_tool_budget_is_enforced(tmp_path: Path, cleanup: list[Container]) -> None:
    def greedy(messages: list[Message]) -> ChatResponse:
        return call_tools(*[("get_workspace", {"workspace_id": "ws-sales-prod"})] * 3)

    def conclude(messages: list[Message]) -> ChatResponse:
        statuses = [r["untrusted_data"]["status"] for r in tool_results(messages).values()]
        assert statuses.count("NOT_ALLOWED") == 1
        return answer({"executive_summary": "x", "insufficient_evidence_reason": "testing budget"})

    incident, _, container, _ = await run(tmp_path, [greedy, conclude], agent_max_tool_calls=2)
    cleanup.append(container)
    assert incident.agent_decision is not None and len(incident.agent_decision.tool_call_ids) == 3
