"""Approval workflow rules (who may approve what, expiry, incident state)."""

from __future__ import annotations

import builtins
from datetime import timedelta

from orqentis.approvals.provider import ApprovalProvider
from orqentis.config.clock import Clock
from orqentis.domain.actions import Action, ActionStatus, PolicyDecision
from orqentis.domain.approvals import ApprovalRequest, ApprovalStatus
from orqentis.domain.common import new_id
from orqentis.domain.incidents import ApprovalState, Incident, IncidentStatus, TimelineKind
from orqentis.domain.principals import Principal, PrincipalKind
from orqentis.incidents.service import IncidentService
from orqentis.observability.audit import AuditTrail


class ApprovalError(Exception):
    code = "APPROVAL_ERROR"


class ApprovalNotFoundError(ApprovalError):
    code = "APPROVAL_NOT_FOUND"


class ApprovalStateError(ApprovalError):
    code = "APPROVAL_NOT_PENDING"


class ApprovalExpiredError(ApprovalError):
    code = "APPROVAL_EXPIRED"


class ApprovalForbiddenError(ApprovalError):
    code = "APPROVAL_FORBIDDEN"


def aggregate_state(approvals: list[ApprovalRequest]) -> ApprovalState:
    statuses = {a.status for a in approvals}
    if not approvals:
        return ApprovalState.NOT_REQUIRED
    for status, state in (
        (ApprovalStatus.PENDING, ApprovalState.PENDING),
        (ApprovalStatus.APPROVED, ApprovalState.APPROVED),
        (ApprovalStatus.EXPIRED, ApprovalState.EXPIRED),
        (ApprovalStatus.REJECTED, ApprovalState.REJECTED),
    ):
        if status in statuses:
            return state
    return ApprovalState.CANCELLED


def update_action_in_incident(incident: Incident, action: Action) -> None:
    for recommendation in incident.recommended_actions:
        for index, existing in enumerate(recommendation.actions):
            if existing.action_id == action.action_id:
                recommendation.actions[index] = action


class ApprovalService:
    def __init__(
        self,
        provider: ApprovalProvider,
        incidents: IncidentService,
        audit: AuditTrail,
        clock: Clock,
        *,
        ttl: timedelta,
    ) -> None:
        self._provider = provider
        self._incidents = incidents
        self._audit = audit
        self._clock = clock
        self._ttl = ttl

    @property
    def provider(self) -> ApprovalProvider:
        return self._provider

    async def get(self, approval_id: str) -> ApprovalRequest:
        approval = await self._provider.get(approval_id)
        if approval is None:
            raise ApprovalNotFoundError(f"Approval {approval_id} not found")
        return await self._expire_if_due(approval)

    async def list(
        self, *, incident_id: str | None = None, status: ApprovalStatus | None = None
    ) -> list[ApprovalRequest]:
        return [await self._expire_if_due(a) for a in await self._provider.list(incident_id=incident_id, status=status)]

    async def find_pending(self, incident_id: str, fingerprint: str) -> ApprovalRequest | None:
        for approval in await self.list(incident_id=incident_id, status=ApprovalStatus.PENDING):
            if approval.action_fingerprint == fingerprint:
                return approval
        return None

    async def request(
        self, action: Action, decision: PolicyDecision, *, incident: Incident, requested_by: Principal
    ) -> ApprovalRequest:
        now = self._clock.now()
        approval = ApprovalRequest(
            approval_id=new_id("APR"),
            incident_id=incident.incident_id,
            proposed_action=action,
            action_fingerprint=action.fingerprint(),
            risk=decision.risk,
            reason="; ".join(decision.reasons),
            required_role=decision.required_role,
            requested_by=requested_by.id,
            requested_at=now,
            expires_at=now + self._ttl,
            policy_decision_id=decision.decision_id,
        )
        action.approval_id = approval.approval_id
        action.status = ActionStatus.AWAITING_APPROVAL
        approval = approval.model_copy(update={"proposed_action": action.model_copy()})
        await self._provider.submit(approval)
        await self._audit.record(
            actor=requested_by.id,
            action="approval.request",
            outcome=ApprovalStatus.PENDING.value,
            incident_id=incident.incident_id,
            trace_id=incident.agent_trace_id,
            details={
                "approval_id": approval.approval_id,
                "action_type": action.action_type.value,
                "target_item_id": action.target_item_id,
                "risk": decision.risk.value,
                "required_role": decision.required_role,
                "fingerprint": approval.action_fingerprint,
            },
        )
        return approval

    async def decide(
        self, approval_id: str, *, principal: Principal, approve: bool, comment: str | None = None
    ) -> ApprovalRequest:
        approval = await self.get(approval_id)
        outcome = "APPROVE" if approve else "REJECT"
        try:
            if approval.status is not ApprovalStatus.PENDING:
                raise ApprovalStateError(f"Approval {approval_id} is {approval.status.value}, not PENDING")
            if principal.kind is not PrincipalKind.HUMAN:
                raise ApprovalForbiddenError("Only human principals can decide approvals (agents cannot approve)")
            if principal.id == approval.requested_by:
                raise ApprovalForbiddenError("The requester cannot approve their own request")
            if not principal.has_role(approval.required_role):
                raise ApprovalForbiddenError(f"Role {approval.required_role} is required to decide this approval")
        except ApprovalError as exc:
            await self._audit.record(
                actor=principal.id,
                action="approval.decide",
                outcome="REFUSED",
                incident_id=approval.incident_id,
                details={"approval_id": approval_id, "requested": outcome, "reason": str(exc), "code": exc.code},
            )
            raise

        now = self._clock.now()
        action = approval.proposed_action.model_copy(
            update={"status": ActionStatus.APPROVED if approve else ActionStatus.REJECTED}
        )
        update: dict[str, object] = {"proposed_action": action, "decision_comment": comment}
        if approve:
            update |= {"status": ApprovalStatus.APPROVED, "approved_by": principal.id, "approved_at": now}
        else:
            update |= {"status": ApprovalStatus.REJECTED, "rejected_by": principal.id, "rejected_at": now}
        approval = approval.model_copy(update=update)
        await self._provider.save(approval)
        await self._audit.record(
            actor=principal.id,
            action="approval.decide",
            outcome=approval.status.value,
            incident_id=approval.incident_id,
            details={"approval_id": approval_id, "comment": comment, "action_type": action.action_type.value},
        )
        await self._sync_incident(
            approval,
            action,
            actor=principal.id,
            summary=f"Approval {approval_id} {approval.status.value} by {principal.id}",
        )
        return approval

    async def save(self, approval: ApprovalRequest) -> None:
        await self._provider.save(approval)

    async def cancel_pending(self, incident_id: str, *, actor: str, reason: str) -> builtins.list[ApprovalRequest]:
        """Cancel open requests, e.g. when an incident is re-investigated and recommendations are replaced."""
        cancelled: builtins.list[ApprovalRequest] = []
        for approval in await self._provider.list(incident_id=incident_id, status=ApprovalStatus.PENDING):
            action = approval.proposed_action.model_copy(update={"status": ActionStatus.CANCELLED})
            approval = approval.model_copy(
                update={"status": ApprovalStatus.CANCELLED, "proposed_action": action, "decision_comment": reason}
            )
            await self._provider.save(approval)
            await self._audit.record(
                actor=actor,
                action="approval.cancel",
                outcome="CANCELLED",
                incident_id=incident_id,
                details={"approval_id": approval.approval_id, "reason": reason},
            )
            cancelled.append(approval)
        return cancelled

    async def _expire_if_due(self, approval: ApprovalRequest) -> ApprovalRequest:
        if approval.status is ApprovalStatus.PENDING and self._clock.now() > approval.expires_at:
            action = approval.proposed_action.model_copy(update={"status": ActionStatus.EXPIRED})
            approval = approval.model_copy(update={"status": ApprovalStatus.EXPIRED, "proposed_action": action})
            await self._provider.save(approval)
            await self._audit.record(
                actor="system:approvals",
                action="approval.expire",
                outcome="EXPIRED",
                incident_id=approval.incident_id,
                details={"approval_id": approval.approval_id},
            )
            await self._sync_incident(
                approval, action, actor="system:approvals", summary=f"Approval {approval.approval_id} expired"
            )
        return approval

    async def _sync_incident(self, approval: ApprovalRequest, action: Action, *, actor: str, summary: str) -> None:
        incident = await self._incidents.repository.get(approval.incident_id)
        if incident is None:
            return
        update_action_in_incident(incident, action)
        incident.approval_state = aggregate_state(await self._provider.list(incident_id=incident.incident_id))
        self._incidents.add_timeline(
            incident, TimelineKind.APPROVAL, summary, actor=actor, metadata={"approval_id": approval.approval_id}
        )
        if incident.status is IncidentStatus.AWAITING_APPROVAL and incident.approval_state in {
            ApprovalState.REJECTED,
            ApprovalState.EXPIRED,
            ApprovalState.CANCELLED,
        }:
            await self._incidents.transition(
                incident, IncidentStatus.DIAGNOSED, actor=actor, reason="No approved remediation pending"
            )
        else:
            await self._incidents.save(incident)
