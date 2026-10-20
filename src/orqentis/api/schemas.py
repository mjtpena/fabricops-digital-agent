"""API response/request shapes that are not domain models."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from orqentis.domain.common import Environment, ItemType
from orqentis.domain.incidents import ApprovalState, BlastRadius, Incident, IncidentStatus
from orqentis.domain.taxonomy import Severity
from orqentis.runtime.investigations import InvestigationJob


class Health(BaseModel):
    """Liveness: the process is up. Says nothing about whether the estate has been loaded (see ``Ready``)."""

    status: str
    version: str
    fabric_mode: str
    reasoner: str
    write_actions_enabled: bool
    time: datetime


class Ready(BaseModel):
    status: Literal["ready"] = "ready"
    scheduler: Literal["disabled", "standby", "leader"]


class NotReady(BaseModel):
    status: Literal["not_ready"] = "not_ready"
    # Exception type plus a redacted first line of its message; the full detail is in the (redacted) logs.
    reason: str
    attempts: int
    retry_in_seconds: float | None = None


class IncidentSummary(BaseModel):
    incident_id: str
    title: str
    status: IncidentStatus
    severity: Severity
    category: str
    detected_at: datetime
    environment: Environment
    workspace_id: str
    primary_item_id: str
    primary_item_name: str | None
    confidence_level: str | None
    confidence_score: float | None
    approval_state: ApprovalState
    report_uri: str | None

    @classmethod
    def of(cls, incident: Incident) -> IncidentSummary:
        return cls(
            incident_id=incident.incident_id,
            title=incident.title,
            status=incident.status,
            severity=incident.severity,
            category=incident.category,
            detected_at=incident.detected_at,
            environment=incident.environment,
            workspace_id=incident.workspace_id,
            primary_item_id=incident.primary_item_id,
            primary_item_name=incident.primary_item_name,
            confidence_level=incident.confidence.level.value if incident.confidence else None,
            confidence_score=incident.confidence.score if incident.confidence else None,
            approval_state=incident.approval_state,
            report_uri=incident.report_uri,
        )


class InvestigationAccepted(InvestigationJob):
    """``202 Accepted`` for ``POST /incidents/{id}/investigate``: the job to poll."""

    status_url: str
    # True when an investigation of this incident was already queued or running and that job is returned.
    already_active: bool = False


class CollectionResult(BaseModel):
    new_events: int
    by_type: dict[str, int]
    incidents_created: list[str]
    collector_errors: dict[str, str]


class DependencyNode(BaseModel):
    item_id: str
    display_name: str
    item_type: ItemType
    workspace_id: str | None
    depth: int
    path: list[str]
    via: str | None


class ItemDependencies(BaseModel):
    item_id: str
    display_name: str
    item_type: ItemType
    upstream: list[DependencyNode]
    downstream: list[DependencyNode]
    blast_radius: BlastRadius


class RunbookSummary(BaseModel):
    id: str
    version: str
    title: str
    category: str
    risk: str
    approval_required: bool
    owner: str
    last_reviewed: str
    path: str


class DecisionRequest(BaseModel):
    comment: str | None = Field(default=None, max_length=1000)
