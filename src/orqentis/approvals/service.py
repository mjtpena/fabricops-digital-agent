"""Approval workflow rules (who may approve what, expiry, incident state)."""

from __future__ import annotations

import asyncio
import builtins
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta

from orqentis.approvals.provider import ApprovalProvider
from orqentis.config.clock import Clock
from orqentis.domain.actions import Action, ActionStatus, PolicyDecision
from orqentis.domain.approvals import ApprovalRequest, ApprovalStatus
from orqentis.domain.common import new_id
from orqentis.domain.incidents import ApprovalState, Incident, IncidentStatus, TimelineKind
from orqentis.domain.principals import Principal, PrincipalKind
from orqentis.incidents.service import IncidentService
from orqentis.notifications import events as notification_events
from orqentis.notifications.sink import NOOP_SINK, NotificationSink
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
        notifications: NotificationSink = NOOP_SINK,
    ) -> None:
        self._provider = provider
        self._incidents = incidents
        self._audit = audit
        self._clock = clock
        self._ttl = ttl
        self._notifications = notifications
        self._locks: dict[str, tuple[asyncio.Lock, int]] = {}

    @property
    def provider(self) -> ApprovalProvider:
        return self._provider

    @asynccontextmanager
    async def exclusive(self, approval_id: str) -> AsyncIterator[None]:
        """Serialise decisions and execution claims for one approval within this process.

        The compare-and-set in ``ApprovalProvider.transition`` is what makes the outcome safe across processes;
        the lock only spares concurrent callers a pointless conflict.
        """
        lock, users = self._locks.get(approval_id) or (asyncio.Lock(), 0)
        self._locks[approval_id] = (lock, users + 1)
        try:
            async with lock:
                yield
        finally:
            lock, users = self._locks[approval_id]
            if users <= 1:
                del self._locks[approval_id]
            else:
                self._locks[approval_id] = (lock, users - 1)

    async def transition(
        self, updated: ApprovalRequest, *, expected_revision: int, enforce_expiry: bool = False
    ) -> ApprovalRequest | None:
        """Compare-and-set a state change (see ``ApprovalProvider.transition``)."""
        return await self._provider.transition(
            updated, expected_revision=expected_revision, enforce_expiry=enforce_expiry
        )

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
        stored = await self._provider.submit(approval)
        if stored.approval_id != approval.approval_id:
            # The store already had a pending request for this very action (a stalled worker and its replacement, or
            # another replica): that one stands, and nothing new was requested, audited or announced.
            action.approval_id = stored.approval_id
            return stored
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
        self._notifications.publish(notification_events.approval_requested(approval, incident_title=incident.title))
        return approval

    async def decide(
        self, approval_id: str, *, principal: Principal, approve: bool, comment: str | None = None
    ) -> ApprovalRequest:
        async with self.exclusive(approval_id):
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

                now = self._clock.now()
                action = approval.proposed_action.model_copy(
                    update={"status": ActionStatus.APPROVED if approve else ActionStatus.REJECTED}
                )
                update: dict[str, object] = {"proposed_action": action, "decision_comment": comment}
                if approve:
                    update |= {"status": ApprovalStatus.APPROVED, "approved_by": principal.id, "approved_at": now}
                else:
                    update |= {"status": ApprovalStatus.REJECTED, "rejected_by": principal.id, "rejected_at": now}
                # Compare-and-set: of two concurrent decisions (even in different processes) exactly one is stored.
                stored = await self._provider.transition(
                    approval.model_copy(update=update), expected_revision=approval.revision, enforce_expiry=approve
                )
                if stored is None:
                    current = await self._provider.get(approval_id)
                    if current is not None and current.revision == approval.revision:
                        # Nobody else wrote: the store (by its own clock) judged the approval expired.
                        raise ApprovalExpiredError(f"Approval {approval_id} has expired")
                    raise ApprovalStateError(f"Approval {approval_id} was decided by another request")
            except ApprovalError as exc:
                await self._audit.record(
                    actor=principal.id,
                    action="approval.decide",
                    outcome="REFUSED",
                    incident_id=approval.incident_id,
                    details={"approval_id": approval_id, "requested": outcome, "reason": str(exc), "code": exc.code},
                )
                raise

            await self._audit.record(
                actor=principal.id,
                action="approval.decide",
                outcome=stored.status.value,
                incident_id=stored.incident_id,
                details={"approval_id": approval_id, "comment": comment, "action_type": action.action_type.value},
            )
            await self._sync_incident(
                stored,
                action,
                actor=principal.id,
                summary=f"Approval {approval_id} {stored.status.value} by {principal.id}",
            )
            return stored

    async def save(self, approval: ApprovalRequest) -> None:
        """Unconditional write, bypassing compare-and-set. For tests; production code uses ``transition``."""
        await self._provider.save(approval)

    async def cancel_pending(self, incident_id: str, *, actor: str, reason: str) -> builtins.list[ApprovalRequest]:
        """Cancel open requests, e.g. when an incident is re-investigated and recommendations are replaced."""
        cancelled: builtins.list[ApprovalRequest] = []
        for approval in await self._provider.list(incident_id=incident_id, status=ApprovalStatus.PENDING):
            action = approval.proposed_action.model_copy(update={"status": ActionStatus.CANCELLED})
            cancelled_request = approval.model_copy(
                update={"status": ApprovalStatus.CANCELLED, "proposed_action": action, "decision_comment": reason}
            )
            stored = await self._provider.transition(cancelled_request, expected_revision=approval.revision)
            if stored is None:
                continue  # decided or expired in the meantime: nothing left to cancel
            approval = stored
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
            expired = approval.model_copy(update={"status": ApprovalStatus.EXPIRED, "proposed_action": action})
            stored = await self._provider.transition(expired, expected_revision=approval.revision)
            if stored is None:
                # Someone decided it first: report what is stored now, not our stale copy.
                return await self._provider.get(approval.approval_id) or approval
            approval = stored
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
