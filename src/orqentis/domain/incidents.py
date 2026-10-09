"""The incident aggregate and its lifecycle."""

from __future__ import annotations

from enum import StrEnum

from pydantic import AwareDatetime, Field

from orqentis.domain.actions import Recommendation
from orqentis.domain.agent import AgentDecision
from orqentis.domain.common import DomainModel, Environment, ItemType, Metadata
from orqentis.domain.confidence import ConfidenceAssessment
from orqentis.domain.evidence import Evidence, Finding
from orqentis.domain.runbooks import RunbookMatch
from orqentis.domain.sla import SLAAssessment
from orqentis.domain.taxonomy import CategoryName, Severity
from orqentis.domain.topology import Dependency


class IncidentStatus(StrEnum):
    DETECTED = "DETECTED"
    INVESTIGATING = "INVESTIGATING"
    DIAGNOSED = "DIAGNOSED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    REMEDIATING = "REMEDIATING"
    VERIFYING = "VERIFYING"
    RESOLVED = "RESOLVED"
    CLOSED = "CLOSED"

    @property
    def is_open(self) -> bool:
        return self not in {IncidentStatus.RESOLVED, IncidentStatus.CLOSED}


ALLOWED_TRANSITIONS: dict[IncidentStatus, frozenset[IncidentStatus]] = {
    IncidentStatus.DETECTED: frozenset({IncidentStatus.INVESTIGATING, IncidentStatus.CLOSED}),
    # Back to DETECTED when an investigation fails part-way.
    IncidentStatus.INVESTIGATING: frozenset({IncidentStatus.DIAGNOSED, IncidentStatus.DETECTED}),
    IncidentStatus.DIAGNOSED: frozenset(
        {
            IncidentStatus.AWAITING_APPROVAL,
            IncidentStatus.INVESTIGATING,
            IncidentStatus.RESOLVED,
            IncidentStatus.CLOSED,
        }
    ),
    IncidentStatus.AWAITING_APPROVAL: frozenset(
        {
            IncidentStatus.REMEDIATING,
            IncidentStatus.DIAGNOSED,
            IncidentStatus.INVESTIGATING,
            IncidentStatus.CLOSED,
        }
    ),
    IncidentStatus.REMEDIATING: frozenset({IncidentStatus.VERIFYING, IncidentStatus.DIAGNOSED}),
    IncidentStatus.VERIFYING: frozenset({IncidentStatus.RESOLVED, IncidentStatus.INVESTIGATING}),
    IncidentStatus.RESOLVED: frozenset({IncidentStatus.CLOSED, IncidentStatus.INVESTIGATING}),
    IncidentStatus.CLOSED: frozenset(),
}


class ApprovalState(StrEnum):
    NOT_EVALUATED = "NOT_EVALUATED"
    NOT_REQUIRED = "NOT_REQUIRED"
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


class TimelineKind(StrEnum):
    TELEMETRY = "TELEMETRY"
    DETECTED = "DETECTED"
    STATUS_CHANGE = "STATUS_CHANGE"
    EVIDENCE = "EVIDENCE"
    FINDING = "FINDING"
    AGENT = "AGENT"
    POLICY = "POLICY"
    APPROVAL = "APPROVAL"
    ACTION = "ACTION"
    NOTE = "NOTE"


class TimelineEntry(DomainModel):
    timestamp: AwareDatetime
    kind: TimelineKind
    summary: str
    actor: str
    evidence_ids: list[str] = Field(default_factory=list)
    metadata: Metadata = Field(default_factory=dict)


class Symptom(DomainModel):
    description: str
    observed_at: AwareDatetime
    source: str
    item_id: str | None = None
    event_id: str | None = None


class ImpactKind(StrEnum):
    FAILURE_POINT = "FAILURE_POINT"
    DIRECT = "DIRECT"
    INDIRECT = "INDIRECT"


class AffectedAsset(DomainModel):
    item_id: str
    item_type: ItemType
    display_name: str
    workspace_id: str | None = None
    environment: Environment = Environment.UNKNOWN
    owner: str | None = None
    depth: int = Field(ge=0)
    impact: ImpactKind
    path: list[str] = Field(default_factory=list)


class BlastRadius(DomainModel):
    origin_item_ids: list[str]
    assets: list[AffectedAsset]
    by_type: dict[str, int] = Field(default_factory=dict)
    workspace_ids: list[str] = Field(default_factory=list)
    data_product_ids: list[str] = Field(default_factory=list)
    max_depth: int = 0
    cross_workspace: bool = False
    summary: str = ""

    @property
    def total(self) -> int:
        return len(self.assets)


class RootCauseCandidate(DomainModel):
    rank: int = Field(ge=1)
    statement: str
    category: CategoryName
    finding_id: str
    evidence_ids: list[str] = Field(min_length=1)
    confidence: ConfidenceAssessment


class RelatedIncidentMatch(DomainModel):
    incident_id: str
    title: str
    category: str
    detected_at: AwareDatetime
    status: IncidentStatus
    similarity: float = Field(ge=0, le=1)
    reasons: list[str] = Field(default_factory=list)
    resolved_with_runbook_id: str | None = None
    resolution_summary: str | None = None


class Incident(DomainModel):
    incident_id: str
    tenant_id: str
    title: str
    status: IncidentStatus = IncidentStatus.DETECTED
    severity: Severity
    category: CategoryName
    detected_at: AwareDatetime
    source: str
    workspace_id: str
    primary_item_id: str
    primary_item_type: ItemType | None = None
    primary_item_name: str | None = None
    environment: Environment = Environment.UNKNOWN
    correlation_id: str | None = None

    symptoms: list[Symptom] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    probable_root_causes: list[RootCauseCandidate] = Field(default_factory=list)
    insufficient_evidence_reason: str | None = None
    dependencies: list[Dependency] = Field(default_factory=list)
    affected_assets: list[AffectedAsset] = Field(default_factory=list)
    blast_radius: BlastRadius | None = None
    sla_impact: list[SLAAssessment] = Field(default_factory=list)
    related_incidents: list[RelatedIncidentMatch] = Field(default_factory=list)
    matched_runbooks: list[RunbookMatch] = Field(default_factory=list)
    recommended_actions: list[Recommendation] = Field(default_factory=list)
    confidence: ConfidenceAssessment | None = None
    approval_state: ApprovalState = ApprovalState.NOT_EVALUATED
    approval_ids: list[str] = Field(default_factory=list)
    timeline: list[TimelineEntry] = Field(default_factory=list)
    agent_trace_id: str | None = None
    agent_decision: AgentDecision | None = None
    resolution_summary: str | None = None
    resolved_with_runbook_id: str | None = None
    report_uri: str | None = None

    created_at: AwareDatetime
    updated_at: AwareDatetime
    metadata: Metadata = Field(default_factory=dict)

    def evidence_by_id(self) -> dict[str, Evidence]:
        return {e.evidence_id: e for e in self.evidence}

    def can_transition_to(self, target: IncidentStatus) -> bool:
        return target in ALLOWED_TRANSITIONS[self.status]
