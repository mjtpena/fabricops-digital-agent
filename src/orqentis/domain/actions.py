"""Remediation actions, recommendations, risk classification and policy decisions."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any

from pydantic import AwareDatetime, Field

from orqentis.domain.common import DomainModel, Environment


class RiskLevel(StrEnum):
    READ_ONLY = "READ_ONLY"
    LOW_RISK = "LOW_RISK"
    MEDIUM_RISK = "MEDIUM_RISK"
    HIGH_RISK = "HIGH_RISK"

    @property
    def rank(self) -> int:
        return {"READ_ONLY": 0, "LOW_RISK": 1, "MEDIUM_RISK": 2, "HIGH_RISK": 3}[self.value]

    @staticmethod
    def highest(levels: list[RiskLevel]) -> RiskLevel:
        return max(levels, key=lambda r: r.rank, default=RiskLevel.READ_ONLY)


class ActionType(StrEnum):
    # Read-only
    INSPECT_LOGS = "INSPECT_LOGS"
    QUERY_DEPENDENCIES = "QUERY_DEPENDENCIES"
    QUERY_TELEMETRY = "QUERY_TELEMETRY"
    MANUAL_STEP = "MANUAL_STEP"
    # Mutations
    RERUN_PIPELINE = "RERUN_PIPELINE"
    CANCEL_JOB = "CANCEL_JOB"
    REFRESH_SEMANTIC_MODEL = "REFRESH_SEMANTIC_MODEL"
    RESTART_ORCHESTRATION = "RESTART_ORCHESTRATION"
    MODIFY_CONFIGURATION = "MODIFY_CONFIGURATION"
    MODIFY_ITEM_DEFINITION = "MODIFY_ITEM_DEFINITION"
    UPDATE_CONNECTION_CREDENTIAL = "UPDATE_CONNECTION_CREDENTIAL"
    SCALE_CAPACITY = "SCALE_CAPACITY"
    DEPLOY_TO_STAGE = "DEPLOY_TO_STAGE"
    CHANGE_PERMISSIONS = "CHANGE_PERMISSIONS"
    DELETE_ITEM = "DELETE_ITEM"

    @property
    def is_mutation(self) -> bool:
        return self not in READ_ONLY_ACTION_TYPES


READ_ONLY_ACTION_TYPES = frozenset(
    {ActionType.INSPECT_LOGS, ActionType.QUERY_DEPENDENCIES, ActionType.QUERY_TELEMETRY, ActionType.MANUAL_STEP}
)


class ActionStatus(StrEnum):
    PROPOSED = "PROPOSED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    DENIED = "DENIED"
    EXPIRED = "EXPIRED"
    EXECUTING = "EXECUTING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    NOT_REQUIRED = "NOT_REQUIRED"


class PolicyEffect(StrEnum):
    ALLOW = "ALLOW"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"
    DENY = "DENY"


class PolicyDecision(DomainModel):
    decision_id: str
    action_id: str
    action_type: ActionType
    environment: Environment
    effect: PolicyEffect
    risk: RiskLevel
    required_role: str | None = None
    reasons: list[str] = Field(default_factory=list)
    matched_rule_ids: list[str] = Field(default_factory=list)
    policy_version: str
    evaluated_at: AwareDatetime


class Action(DomainModel):
    action_id: str
    incident_id: str
    action_type: ActionType
    title: str
    description: str = ""
    target_item_id: str | None = None
    target_display_name: str | None = None
    workspace_id: str | None = None
    environment: Environment = Environment.UNKNOWN
    parameters: dict[str, Any] = Field(default_factory=dict)
    idempotent: bool = False
    # Risk is assigned by the policy engine, never by the agent.
    risk: RiskLevel | None = None
    status: ActionStatus = ActionStatus.PROPOSED
    requested_by: str
    runbook_id: str | None = None
    policy_decision: PolicyDecision | None = None
    approval_id: str | None = None
    created_at: AwareDatetime
    executed_at: AwareDatetime | None = None
    result: dict[str, Any] | None = None

    @property
    def is_mutation(self) -> bool:
        return self.action_type.is_mutation

    def fingerprint(self) -> str:
        """Hash of what would actually be executed; approvals are bound to it."""
        material = {
            "incident_id": self.incident_id,
            "action_type": self.action_type.value,
            "target_item_id": self.target_item_id,
            "workspace_id": self.workspace_id,
            "environment": self.environment.value,
            "parameters": self.parameters,
        }
        canonical = json.dumps(material, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode()).hexdigest()


class Recommendation(DomainModel):
    recommendation_id: str
    incident_id: str
    title: str
    summary: str
    runbook_id: str | None = None
    runbook_version: str | None = None
    steps: list[str] = Field(default_factory=list)
    actions: list[Action] = Field(default_factory=list)
    risk: RiskLevel
    approval_required: bool
    validation_plan: list[str] = Field(default_factory=list)
    rollback: list[str] = Field(default_factory=list)
    escalation: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    produced_by: str
    created_at: AwareDatetime
