"""Approval requests binding a human decision to an exact proposed action."""

from __future__ import annotations

from enum import StrEnum

from pydantic import AwareDatetime

from orqentis.domain.actions import Action, RiskLevel
from orqentis.domain.common import DomainModel


class ApprovalStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


class ApprovalRequest(DomainModel):
    approval_id: str
    incident_id: str
    proposed_action: Action
    action_fingerprint: str
    risk: RiskLevel
    reason: str
    required_role: str | None = None
    requested_by: str
    requested_at: AwareDatetime
    expires_at: AwareDatetime
    status: ApprovalStatus = ApprovalStatus.PENDING
    approved_by: str | None = None
    approved_at: AwareDatetime | None = None
    rejected_by: str | None = None
    rejected_at: AwareDatetime | None = None
    decision_comment: str | None = None
    policy_decision_id: str | None = None
    # Optimistic-concurrency counter. Every state change goes through ApprovalProvider.transition, which stores
    # the change only if the stored revision is still the one the caller read, and then bumps it.
    revision: int = 0
