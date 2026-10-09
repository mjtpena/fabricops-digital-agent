"""Policy decisions (separate from, and not overridable by, the agent)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from orqentis.config.clock import FixedClock
from orqentis.domain.actions import Action, ActionType, PolicyEffect, RiskLevel
from orqentis.domain.common import Environment
from orqentis.policies.engine import MVP_INVARIANT_RULE, PolicyEngine, PolicyMatch, PolicyRule, PolicySet

CLOCK = FixedClock(datetime(2026, 10, 5, tzinfo=UTC))


def action(
    action_type: ActionType,
    env: Environment = Environment.PROD,
    *,
    idempotent: bool = True,
    risk: RiskLevel | None = None,
) -> Action:
    return Action(
        action_id="ACT-1",
        incident_id="INC-1",
        action_type=action_type,
        title="t",
        target_item_id="x",
        environment=env,
        idempotent=idempotent,
        risk=risk,
        requested_by="agent:FabricOpsInvestigator",
        created_at=CLOCK.now(),
    )


@pytest.fixture
def engine() -> PolicyEngine:
    return PolicyEngine.default(CLOCK)


@pytest.mark.parametrize(
    "action_type",
    [ActionType.INSPECT_LOGS, ActionType.QUERY_DEPENDENCIES, ActionType.QUERY_TELEMETRY, ActionType.MANUAL_STEP],
)
def test_read_only_runs_automatically(engine: PolicyEngine, action_type: ActionType) -> None:
    decision = engine.evaluate(action(action_type))
    assert decision.effect is PolicyEffect.ALLOW
    assert decision.risk is RiskLevel.READ_ONLY
    assert decision.required_role is None


def test_production_write_requires_operator_approval(engine: PolicyEngine) -> None:
    decision = engine.evaluate(action(ActionType.RERUN_PIPELINE))
    assert decision.effect is PolicyEffect.REQUIRE_APPROVAL
    assert decision.risk is RiskLevel.LOW_RISK
    assert decision.required_role == "FabricOps.Operator"
    assert "POL-020-production-write-approval" in decision.matched_rule_ids


def test_non_production_write_also_requires_approval_in_mvp(engine: PolicyEngine) -> None:
    assert (
        engine.evaluate(action(ActionType.REFRESH_SEMANTIC_MODEL, Environment.DEV)).effect
        is PolicyEffect.REQUIRE_APPROVAL
    )


def test_delete_is_prohibited_everywhere(engine: PolicyEngine) -> None:
    for env in Environment:
        decision = engine.evaluate(action(ActionType.DELETE_ITEM, env))
        assert decision.effect is PolicyEffect.DENY
        assert decision.risk is RiskLevel.HIGH_RISK


def test_production_permission_change_is_prohibited(engine: PolicyEngine) -> None:
    assert engine.evaluate(action(ActionType.CHANGE_PERMISSIONS)).effect is PolicyEffect.DENY
    dev = engine.evaluate(action(ActionType.CHANGE_PERMISSIONS, Environment.DEV))
    assert dev.effect is PolicyEffect.REQUIRE_APPROVAL
    assert dev.required_role == "FabricOps.PlatformAdmin"


@pytest.mark.parametrize("action_type", [ActionType.SCALE_CAPACITY, ActionType.DEPLOY_TO_STAGE])
def test_high_risk_requires_platform_admin(engine: PolicyEngine, action_type: ActionType) -> None:
    decision = engine.evaluate(action(action_type))
    assert decision.effect is PolicyEffect.REQUIRE_APPROVAL
    assert decision.risk is RiskLevel.HIGH_RISK
    assert decision.required_role == "FabricOps.PlatformAdmin"


@pytest.mark.parametrize(
    ("action_type", "risk"),
    [
        (ActionType.MODIFY_CONFIGURATION, RiskLevel.MEDIUM_RISK),
        (ActionType.RESTART_ORCHESTRATION, RiskLevel.MEDIUM_RISK),
        (ActionType.UPDATE_CONNECTION_CREDENTIAL, RiskLevel.MEDIUM_RISK),
    ],
)
def test_medium_risk_classification(engine: PolicyEngine, action_type: ActionType, risk: RiskLevel) -> None:
    assert engine.evaluate(action(action_type)).risk is risk


def test_non_idempotent_rerun_is_medium_risk(engine: PolicyEngine) -> None:
    assert engine.evaluate(action(ActionType.RERUN_PIPELINE, idempotent=False)).risk is RiskLevel.MEDIUM_RISK


def test_agent_supplied_risk_is_ignored(engine: PolicyEngine) -> None:
    claimed = action(ActionType.DELETE_ITEM, risk=RiskLevel.READ_ONLY)
    decision = engine.evaluate(claimed)
    assert decision.risk is RiskLevel.HIGH_RISK
    assert decision.effect is PolicyEffect.DENY


def test_every_action_type_is_classified(engine: PolicyEngine) -> None:
    for action_type in ActionType:
        assert engine.classify_risk(action(action_type)) in RiskLevel


def test_mvp_invariant_overrides_a_permissive_policy() -> None:
    permissive = PolicySet(
        version="test",
        role_hierarchy=["Ops"],
        risk_classification={t: RiskLevel.LOW_RISK for t in ActionType},
        rules=[PolicyRule(id="allow-all", description="misconfigured", match=PolicyMatch(), effect=PolicyEffect.ALLOW)],
    )
    decision = PolicyEngine(permissive, CLOCK).evaluate(action(ActionType.RERUN_PIPELINE))
    assert decision.effect is PolicyEffect.REQUIRE_APPROVAL
    assert MVP_INVARIANT_RULE in decision.matched_rule_ids
    assert decision.required_role == "Ops"


def test_unmatched_action_falls_back_to_default_deny() -> None:
    empty = PolicySet(version="test", role_hierarchy=["Ops"], risk_classification={}, rules=[])
    decision = PolicyEngine(empty, CLOCK).evaluate(action(ActionType.RERUN_PIPELINE))
    assert decision.effect is PolicyEffect.DENY
    assert decision.risk is RiskLevel.HIGH_RISK  # unclassified action types fail closed


def test_unknown_role_in_rule_is_rejected() -> None:
    bad = PolicySet(
        version="x",
        role_hierarchy=["Ops"],
        risk_classification={},
        rules=[
            PolicyRule(
                id="r",
                description="d",
                match=PolicyMatch(),
                effect=PolicyEffect.REQUIRE_APPROVAL,
                required_role="Ghost",
            )
        ],
    )
    with pytest.raises(ValueError, match="Ghost"):
        PolicyEngine(bad, CLOCK)


def test_tool_allowlist_excludes_approval_and_execution(engine: PolicyEngine) -> None:
    allow = engine.tool_allowlist
    assert "get_pipeline_run" in allow and "request_pipeline_rerun" in allow
    assert not {t for t in allow if "approve" in t or "execute" in t or "delete" in t}
