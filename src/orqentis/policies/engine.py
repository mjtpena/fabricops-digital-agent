"""Deterministic, declarative policy evaluation. Separate from (and not overridable by) the LLM."""

from __future__ import annotations

from importlib import resources
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from orqentis.config.clock import Clock
from orqentis.domain.actions import Action, ActionType, PolicyDecision, PolicyEffect, RiskLevel
from orqentis.domain.common import Environment, new_id

MVP_INVARIANT_RULE = "MVP-INVARIANT-mutations-require-approval"


class PolicyMatch(BaseModel):
    action_types: list[ActionType] | None = None
    risks: list[RiskLevel] | None = None
    environments: list[Environment] | None = None
    mutation: bool | None = None

    def matches(self, action: Action, risk: RiskLevel) -> bool:
        return (
            (self.action_types is None or action.action_type in self.action_types)
            and (self.risks is None or risk in self.risks)
            and (self.environments is None or action.environment in self.environments)
            and (self.mutation is None or action.is_mutation == self.mutation)
        )


class PolicyRule(BaseModel):
    id: str
    description: str
    match: PolicyMatch
    effect: PolicyEffect
    required_role: str | None = None


class PolicySet(BaseModel):
    version: str
    default_effect: PolicyEffect = PolicyEffect.DENY
    role_hierarchy: list[str] = Field(min_length=1)
    risk_classification: dict[ActionType, RiskLevel]
    rules: list[PolicyRule]
    agent_tool_allowlist: list[str] = Field(default_factory=list)


_PRECEDENCE = {PolicyEffect.DENY: 3, PolicyEffect.REQUIRE_APPROVAL: 2, PolicyEffect.ALLOW: 1}


class PolicyEngine:
    def __init__(self, policy: PolicySet, clock: Clock) -> None:
        unknown_roles = {r.required_role for r in policy.rules if r.required_role} - set(policy.role_hierarchy)
        if unknown_roles:
            raise ValueError(f"Policy rules reference roles missing from role_hierarchy: {sorted(unknown_roles)}")
        self._policy = policy
        self._clock = clock

    @classmethod
    def from_file(cls, path: Path, clock: Clock) -> PolicyEngine:
        return cls(PolicySet.model_validate(yaml.safe_load(path.read_text(encoding="utf-8"))), clock)

    @classmethod
    def default(cls, clock: Clock) -> PolicyEngine:
        text = resources.files("orqentis.policies").joinpath("default_policy.yaml").read_text(encoding="utf-8")
        return cls(PolicySet.model_validate(yaml.safe_load(text)), clock)

    @property
    def version(self) -> str:
        return self._policy.version

    @property
    def tool_allowlist(self) -> frozenset[str]:
        return frozenset(self._policy.agent_tool_allowlist)

    def classify_risk(self, action: Action) -> RiskLevel:
        # Unclassified action types are treated as the highest risk (fail closed).
        risk = self._policy.risk_classification.get(action.action_type, RiskLevel.HIGH_RISK)
        if action.action_type is ActionType.RERUN_PIPELINE and not action.idempotent:
            # Only an idempotent retry qualifies as LOW_RISK.
            risk = RiskLevel.highest([risk, RiskLevel.MEDIUM_RISK])
        return risk

    def _highest_role(self, roles: list[str]) -> str | None:
        ranked = [r for r in self._policy.role_hierarchy if r in roles]
        return ranked[-1] if ranked else None

    def evaluate(self, action: Action) -> PolicyDecision:
        risk = self.classify_risk(action)
        matched = [rule for rule in self._policy.rules if rule.match.matches(action, risk)]
        if matched:
            effect = max((r.effect for r in matched), key=lambda e: _PRECEDENCE[e])
            reasons = [f"{r.id}: {r.description}" for r in matched if r.effect is effect]
        else:
            effect = self._policy.default_effect
            reasons = [f"No rule matched; default effect {effect.value}"]
        matched_ids = [r.id for r in matched]
        role = self._highest_role(
            [r.required_role for r in matched if r.effect is PolicyEffect.REQUIRE_APPROVAL and r.required_role]
        )
        if action.is_mutation and effect is PolicyEffect.ALLOW:
            effect = PolicyEffect.REQUIRE_APPROVAL
            reasons.append(f"{MVP_INVARIANT_RULE}: only READ_ONLY operations run automatically")
            matched_ids.append(MVP_INVARIANT_RULE)
        if effect is PolicyEffect.REQUIRE_APPROVAL and role is None:
            role = self._policy.role_hierarchy[0]
        return PolicyDecision(
            decision_id=new_id("POL"),
            action_id=action.action_id,
            action_type=action.action_type,
            environment=action.environment,
            effect=effect,
            risk=risk,
            required_role=role if effect is PolicyEffect.REQUIRE_APPROVAL else None,
            reasons=reasons,
            matched_rule_ids=matched_ids,
            policy_version=self._policy.version,
            evaluated_at=self._clock.now(),
        )
