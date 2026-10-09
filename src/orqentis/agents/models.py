"""Inputs and outputs of the investigator agent."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import AwareDatetime, BaseModel, Field

from orqentis.domain.agent import ModelUsage
from orqentis.domain.common import Environment, ItemType
from orqentis.domain.deployment import DeploymentComparison
from orqentis.domain.evidence import Evidence, FindingCategory, StatementKind
from orqentis.domain.incidents import BlastRadius, RelatedIncidentMatch
from orqentis.domain.runbooks import RunbookMatch
from orqentis.domain.sla import SLAAssessment
from orqentis.incidents.classification import SignatureMatch
from orqentis.incidents.history import RecurringPattern
from orqentis.tools.models import ToolCallRecord


class ErrorObservation(BaseModel):
    evidence_id: str
    message: str
    error_code: str | None = None
    activity_name: str | None = None
    activity_type: str | None = None
    target_item_id: str | None = None
    observed_at: AwareDatetime | None = None
    signature: SignatureMatch | None = None


class SchemaChangeObservation(BaseModel):
    evidence_id: str
    item_id: str
    item_name: str
    table: str | None = None
    removed_columns: list[str] = Field(default_factory=list)
    added_columns: list[str] = Field(default_factory=list)
    observed_at: AwareDatetime
    upstream_path: list[str] = Field(default_factory=list)


class CredentialObservation(BaseModel):
    evidence_id: str
    item_id: str
    item_name: str
    connection_id: str | None = None
    client_app: str | None = None
    expires_at: AwareDatetime
    expired_before_failure: bool


class ContendingWorkload(BaseModel):
    item_id: str
    display_name: str
    workspace_id: str
    workspace_name: str
    environment: Environment
    peak_cu_percent: float
    cross_workspace: bool
    in_incident_lineage: bool


class CapacityObservation(BaseModel):
    evidence_id: str
    capacity_id: str
    capacity_name: str
    sku: str | None = None
    environment: Environment
    peak_cu_percent: float | None = None
    max_background_rejection_percent: float | None = None
    max_interactive_delay_percent: float | None = None
    throttling_detected: bool = False
    failure_during_throttling: bool = False
    contending: list[ContendingWorkload] = Field(default_factory=list)


class DeploymentObservation(BaseModel):
    evidence_id: str
    item_id: str
    item_name: str
    comparison: DeploymentComparison


class InvestigationContext(BaseModel):
    """Deterministic analysis computed by the workflow; every entry points at evidence."""

    failure_time: AwareDatetime
    errors: list[ErrorObservation] = Field(default_factory=list)
    primary_signature: SignatureMatch | None = None
    failure_point_ids: list[str] = Field(default_factory=list)
    upstream_evidence_ids: list[str] = Field(default_factory=list)
    downstream_evidence_ids: list[str] = Field(default_factory=list)
    schema_changes: list[SchemaChangeObservation] = Field(default_factory=list)
    credentials: list[CredentialObservation] = Field(default_factory=list)
    capacity: CapacityObservation | None = None
    deployments: list[DeploymentObservation] = Field(default_factory=list)
    blast_radius: BlastRadius | None = None
    blast_radius_evidence_id: str | None = None
    sla: list[SLAAssessment] = Field(default_factory=list)
    sla_evidence_id: str | None = None
    related_incidents: list[RelatedIncidentMatch] = Field(default_factory=list)
    history_evidence_id: str | None = None
    pattern: RecurringPattern | None = None
    pattern_evidence_id: str | None = None
    runbooks: list[RunbookMatch] = Field(default_factory=list)
    runbook_evidence_id: str | None = None
    refresh_evidence_ids: list[str] = Field(default_factory=list)


class PrimaryItemView(BaseModel):
    item_id: str
    display_name: str
    item_type: ItemType | None
    workspace_id: str
    environment: Environment


class EvidencePackage(BaseModel):
    incident_id: str
    title: str
    detected_category: str
    detected_at: AwareDatetime
    primary_item: PrimaryItemView
    evidence: list[Evidence]
    context: InvestigationContext

    def for_model(self) -> dict[str, Any]:
        """Compact JSON view for an LLM (raw references and internal ids trimmed)."""
        return {
            "incident_id": self.incident_id,
            "title": self.title,
            "detected_category": self.detected_category,
            "detected_at": self.detected_at.isoformat(),
            "primary_item": self.primary_item.model_dump(mode="json"),
            "evidence": [
                {
                    "evidence_id": e.evidence_id,
                    "evidence_type": e.evidence_type.value,
                    "source": e.source,
                    "resource_id": e.resource_id,
                    "observed_at": e.observed_at.isoformat() if e.observed_at else None,
                    "summary": e.summary,
                    "payload": e.structured_payload,
                    "security_flags": e.untrusted_flags,
                }
                for e in self.evidence
            ],
            "deterministic_analysis": self.context.model_dump(mode="json", exclude_none=True),
        }


class ProposedFinding(BaseModel):
    statement: str = Field(max_length=1200)
    category: FindingCategory
    evidence_ids: list[str] = Field(default_factory=list)
    reasoning_summary: str = Field(default="", max_length=1200)
    kind: StatementKind = StatementKind.INFERENCE


class ProposedRootCause(BaseModel):
    statement: str = Field(max_length=1200)
    category: str
    evidence_ids: list[str] = Field(default_factory=list)
    reasoning_summary: str = Field(default="", max_length=1200)


class AgentDiagnosis(BaseModel):
    """The only output shape accepted from a reasoner (rule-based or LLM)."""

    executive_summary: str = Field(max_length=2000)
    root_cause: ProposedRootCause | None = None
    insufficient_evidence_reason: str | None = Field(default=None, max_length=1200)
    findings: list[ProposedFinding] = Field(default_factory=list, max_length=20)
    recommended_runbook_id: str | None = None


@dataclass
class ReasonerOutput:
    diagnosis: AgentDiagnosis
    reasoner: str
    model: str | None = None
    usage: ModelUsage | None = None
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
