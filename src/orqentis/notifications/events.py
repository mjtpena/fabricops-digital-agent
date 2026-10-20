"""Builders for the notification events. One function per business event; the event id makes each one idempotent."""

from __future__ import annotations

from datetime import UTC, datetime

from orqentis.domain.actions import Action
from orqentis.domain.approvals import ApprovalRequest
from orqentis.domain.incidents import Incident
from orqentis.domain.principals import Principal
from orqentis.domain.taxonomy import Severity
from orqentis.notifications.model import Facts, NotificationEvent, NotificationKind


def _utc(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def _target(action: Action) -> str:
    return action.target_display_name or action.target_item_id or "-"


def incident_opened(incident: Incident, *, occurred_at: datetime) -> NotificationEvent:
    facts: Facts = (
        ("Severity", incident.severity.value),
        ("Environment", incident.environment.value),
        ("Category", incident.category),
        ("Item", incident.primary_item_name or incident.primary_item_id),
        ("Detected", _utc(incident.detected_at)),
        ("Rule", str(incident.metadata.get("detection_rule", "-"))),
    )
    return NotificationEvent(
        event_id=f"incident-opened:{incident.incident_id}",
        kind=NotificationKind.INCIDENT_OPENED,
        occurred_at=occurred_at,
        incident_id=incident.incident_id,
        headline=f"Incident {incident.incident_id} opened: {incident.title}",
        severity=incident.severity.value,
        environment=incident.environment.value,
        facts=facts,
    )


def incident_escalated(
    incident: Incident, *, previous: Severity, reason: str, occurred_at: datetime
) -> NotificationEvent:
    facts: Facts = (
        ("Severity", f"{previous.value} -> {incident.severity.value}"),
        ("Reason", reason),
        ("Environment", incident.environment.value),
        ("Item", incident.primary_item_name or incident.primary_item_id),
        ("Status", incident.status.value),
    )
    return NotificationEvent(
        # One escalation per incident and severity: a re-investigation that raises the same level again is not news.
        event_id=f"incident-escalated:{incident.incident_id}:{incident.severity.value}",
        kind=NotificationKind.INCIDENT_ESCALATED,
        occurred_at=occurred_at,
        incident_id=incident.incident_id,
        headline=f"Incident {incident.incident_id} escalated to {incident.severity.value}: {incident.title}",
        severity=incident.severity.value,
        environment=incident.environment.value,
        facts=facts,
    )


def approval_requested(approval: ApprovalRequest, *, incident_title: str | None) -> NotificationEvent:
    action = approval.proposed_action
    facts: Facts = (
        ("Action", f"{action.action_type.value}: {action.title}"),
        ("Target", _target(action)),
        ("Environment", action.environment.value),
        ("Risk", approval.risk.value),
        ("Required role", approval.required_role or "-"),
        ("Expires", _utc(approval.expires_at)),
        ("Requested by", approval.requested_by),
        ("Incident", f"{approval.incident_id}" + (f": {incident_title}" if incident_title else "")),
    )
    return NotificationEvent(
        event_id=f"approval-requested:{approval.approval_id}",
        kind=NotificationKind.APPROVAL_REQUESTED,
        occurred_at=approval.requested_at,
        incident_id=approval.incident_id,
        approval_id=approval.approval_id,
        headline=f"Approval requested: {action.action_type.value} on {_target(action)}",
        severity=None,
        environment=action.environment.value,
        facts=facts,
    )


def action_outcome(
    approval: ApprovalRequest,
    action: Action,
    *,
    executed_by: Principal,
    occurred_at: datetime,
    succeeded: bool,
    detail: str,
) -> NotificationEvent:
    """An approved action ran (``succeeded``) or did not (failed, or its outcome is unknown)."""
    kind = NotificationKind.ACTION_EXECUTED if succeeded else NotificationKind.ACTION_FAILED
    facts: Facts = (
        ("Action", f"{action.action_type.value}: {action.title}"),
        ("Target", _target(action)),
        ("Environment", action.environment.value),
        ("Executed by", executed_by.display_name or executed_by.id),
        ("Approved by", approval.approved_by or "-"),
        ("Outcome", detail),
        ("Incident", approval.incident_id),
    )
    verb = "executed" if succeeded else "failed"
    return NotificationEvent(
        event_id=f"{'action-executed' if succeeded else 'action-failed'}:{approval.approval_id}",
        kind=kind,
        occurred_at=occurred_at,
        incident_id=approval.incident_id,
        approval_id=approval.approval_id,
        headline=f"Action {verb}: {action.action_type.value} on {_target(action)}",
        severity=None,
        environment=action.environment.value,
        facts=facts,
    )
