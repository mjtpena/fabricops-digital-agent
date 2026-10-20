"""ActionGateway: policy evaluation, approval binding and (only then) execution.

* ``submit`` never mutates anything. Mutations come back as ``ACTION_REQUIRES_APPROVAL``
  (or ``ACTION_DENIED``).
* ``execute`` runs a mutation only for an APPROVED, unexpired approval whose fingerprint still
  matches the action, decided by a human with the required role, while the write capability is enabled.
  The CURRENT policy is re-evaluated at execution time (deny, risk escalation and its required role), and the
  single use is claimed with a compare-and-set persisted before the write call.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel

from orqentis.approvals.service import ApprovalService, update_action_in_incident
from orqentis.config.clock import Clock
from orqentis.domain.actions import Action, ActionStatus, PolicyDecision, PolicyEffect
from orqentis.domain.approvals import ApprovalRequest, ApprovalStatus
from orqentis.domain.incidents import Incident, IncidentStatus, TimelineKind
from orqentis.domain.principals import Principal, PrincipalKind
from orqentis.fabric.errors import FabricError
from orqentis.fabric.ports import FabricWriteClient
from orqentis.incidents.service import IncidentService
from orqentis.observability.audit import AuditTrail
from orqentis.observability.logging import get_logger
from orqentis.policies.engine import PolicyEngine

_log = get_logger(__name__)


class GatewayStatus(StrEnum):
    ALLOWED_READ_ONLY = "ALLOWED_READ_ONLY"
    ACTION_REQUIRES_APPROVAL = "ACTION_REQUIRES_APPROVAL"
    ACTION_DENIED = "ACTION_DENIED"
    EXECUTED = "EXECUTED"
    EXECUTION_FAILED = "EXECUTION_FAILED"


class ActionOutcome(BaseModel):
    status: GatewayStatus
    action: Action
    decision: PolicyDecision
    approval: ApprovalRequest | None = None
    message: str


class ActionBlockedError(Exception):
    """A mutation was attempted without satisfying the approval boundary."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ActionGateway:
    def __init__(
        self,
        policy: PolicyEngine,
        approvals: ApprovalService,
        write_client: FabricWriteClient,
        incidents: IncidentService,
        audit: AuditTrail,
        clock: Clock,
        *,
        write_enabled: bool,
    ) -> None:
        self._policy = policy
        self._approvals = approvals
        self._write = write_client
        self._incidents = incidents
        self._audit = audit
        self._clock = clock
        self._write_enabled = write_enabled

    async def submit(self, action: Action, *, principal: Principal, incident: Incident) -> ActionOutcome:
        """Evaluate policy and, where required, open an approval request. Never executes a mutation."""
        decision = self._policy.evaluate(action)
        action.risk = decision.risk
        action.policy_decision = decision
        await self._audit.record(
            actor=principal.id,
            action="policy.evaluate",
            outcome=decision.effect.value,
            incident_id=incident.incident_id,
            trace_id=incident.agent_trace_id,
            details={
                "action_id": action.action_id,
                "action_type": action.action_type.value,
                "environment": action.environment.value,
                "risk": decision.risk.value,
                "required_role": decision.required_role,
                "rules": decision.matched_rule_ids,
                "policy_version": decision.policy_version,
            },
        )
        self._incidents.add_timeline(
            incident,
            TimelineKind.POLICY,
            f"Policy {decision.effect.value} for {action.action_type.value} on "
            f"{action.target_display_name or action.target_item_id} ({decision.risk.value})",
            actor="policy-engine",
            metadata={"decision_id": decision.decision_id},
        )
        if decision.effect is PolicyEffect.DENY:
            action.status = ActionStatus.DENIED
            return ActionOutcome(
                status=GatewayStatus.ACTION_DENIED,
                action=action,
                decision=decision,
                message="Action is prohibited by policy: " + "; ".join(decision.reasons),
            )
        if decision.effect is PolicyEffect.ALLOW:
            action.status = ActionStatus.NOT_REQUIRED
            return ActionOutcome(
                status=GatewayStatus.ALLOWED_READ_ONLY,
                action=action,
                decision=decision,
                message="Read-only action; no approval or mutation required",
            )
        existing = await self._approvals.find_pending(incident.incident_id, action.fingerprint())
        if existing is not None:
            action.approval_id = existing.approval_id
            action.status = ActionStatus.AWAITING_APPROVAL
            approval = existing
        else:
            approval = await self._approvals.request(action, decision, incident=incident, requested_by=principal)
        return ActionOutcome(
            status=GatewayStatus.ACTION_REQUIRES_APPROVAL,
            action=action,
            decision=decision,
            approval=approval,
            message=f"ACTION_REQUIRES_APPROVAL: approval {approval.approval_id} must be granted by "
            f"{approval.required_role} before execution",
        )

    async def _block(self, approval: ApprovalRequest, principal: Principal, code: str, message: str) -> None:
        await self._audit.record(
            actor=principal.id,
            action="action.execute",
            outcome="BLOCKED",
            incident_id=approval.incident_id,
            details={"approval_id": approval.approval_id, "code": code, "reason": message},
        )
        _log.warning("mutation blocked", extra={"approval_id": approval.approval_id, "code": code})
        raise ActionBlockedError(code, message)

    async def execute(self, approval_id: str, *, principal: Principal) -> ActionOutcome:
        # Everything up to and including the claim runs under the approval's lock. The claim itself is a
        # compare-and-set that is persisted BEFORE the write call, so of any number of concurrent executes
        # (in this or another process) exactly one reaches the write client.
        async with self._approvals.exclusive(approval_id):
            approval = await self._approvals.get(approval_id)
            action = approval.proposed_action
            if principal.kind is not PrincipalKind.HUMAN:
                await self._block(
                    approval,
                    principal,
                    "PRINCIPAL_NOT_HUMAN",
                    "Only a human principal can trigger an approved mutation",
                )
            if approval.status is not ApprovalStatus.APPROVED:
                await self._block(
                    approval,
                    principal,
                    "ACTION_REQUIRES_APPROVAL",
                    f"Approval {approval_id} is {approval.status.value}; execution requires APPROVED",
                )
            if self._clock.now() > approval.expires_at:
                await self._block(approval, principal, "APPROVAL_EXPIRED", f"Approval {approval_id} has expired")
            if action.fingerprint() != approval.action_fingerprint:
                await self._block(
                    approval, principal, "ACTION_FINGERPRINT_MISMATCH", "The action differs from what was approved"
                )
            if not principal.has_role(approval.required_role):
                await self._block(
                    approval,
                    principal,
                    "ROLE_REQUIRED",
                    f"Role {approval.required_role} is required to execute this action",
                )
            if action.status in {ActionStatus.EXECUTING, ActionStatus.SUCCEEDED, ActionStatus.FAILED}:
                await self._block(approval, principal, "ALREADY_EXECUTED", "This approval has already been used")
            # Re-evaluate the CURRENT policy: it may have changed since the approval was granted.
            decision = self._policy.evaluate(action)
            if decision.effect is PolicyEffect.DENY:
                await self._block(approval, principal, "ACTION_DENIED", "Policy now prohibits this action")
            if decision.risk.rank > approval.risk.rank:
                await self._block(
                    approval,
                    principal,
                    "POLICY_RISK_ESCALATED",
                    f"The current policy classifies this action as {decision.risk.value}, higher than the "
                    f"{approval.risk.value} that was approved; request a new approval",
                )
            if not principal.has_role(decision.required_role):
                await self._block(
                    approval,
                    principal,
                    "ROLE_REQUIRED",
                    f"Role {decision.required_role} is required by the current policy to execute this action",
                )
            if not self._write_enabled:
                await self._block(
                    approval,
                    principal,
                    "WRITE_CAPABILITY_NOT_GRANTED",
                    "The write capability boundary is closed (ORQENTIS_ENABLE_WRITE_ACTIONS=false)",
                )

            incident = await self._incidents.get(approval.incident_id)
            if incident.status is IncidentStatus.AWAITING_APPROVAL:
                await self._incidents.transition(
                    incident,
                    IncidentStatus.REMEDIATING,
                    actor=principal.id,
                    reason=f"Executing approved action {action.action_type.value}",
                )
            action = action.model_copy(update={"status": ActionStatus.EXECUTING})
            claimed = await self._approvals.transition(
                approval.model_copy(update={"proposed_action": action}), expected_revision=approval.revision
            )
            if claimed is None:
                await self._block(approval, principal, "ALREADY_EXECUTED", "This approval has already been used")
            assert claimed is not None  # _block raises

        try:
            result = await self._write.execute(action, approval=claimed)
        except FabricError as exc:
            action = action.model_copy(
                update={"status": ActionStatus.FAILED, "result": exc.to_dict(), "executed_at": self._clock.now()}
            )
            status, message = GatewayStatus.EXECUTION_FAILED, f"Execution failed: {exc.message}"
        except Exception:
            # The outcome is unknown, so the claim stays EXECUTING: a retry must not repeat a write that may
            # have happened. A person reconciles it.
            await self._audit.record(
                actor=principal.id,
                action="action.execute",
                outcome="UNKNOWN",
                incident_id=incident.incident_id,
                details={"approval_id": approval_id, "action_type": action.action_type.value},
            )
            raise
        else:
            action = action.model_copy(
                update={"status": ActionStatus.SUCCEEDED, "result": result, "executed_at": self._clock.now()}
            )
            status, message = GatewayStatus.EXECUTED, "Action executed"
        finished = await self._approvals.transition(
            claimed.model_copy(update={"proposed_action": action}), expected_revision=claimed.revision
        )
        approval = finished or claimed.model_copy(update={"proposed_action": action})
        await self._audit.record(
            actor=principal.id,
            action="action.execute",
            outcome=action.status.value,
            incident_id=incident.incident_id,
            details={
                "approval_id": approval_id,
                "action_type": action.action_type.value,
                "target_item_id": action.target_item_id,
                "result": action.result,
            },
        )
        incident = await self._incidents.get(approval.incident_id)
        update_action_in_incident(incident, action)
        self._incidents.add_timeline(
            incident,
            TimelineKind.ACTION,
            f"{action.action_type.value}: {message}",
            actor=principal.id,
            metadata={"approval_id": approval_id},
        )
        if incident.status is IncidentStatus.REMEDIATING:
            target = IncidentStatus.VERIFYING if status is GatewayStatus.EXECUTED else IncidentStatus.DIAGNOSED
            await self._incidents.transition(incident, target, actor=principal.id, reason=message)
        else:
            await self._incidents.save(incident)
        return ActionOutcome(status=status, action=action, decision=decision, approval=approval, message=message)
