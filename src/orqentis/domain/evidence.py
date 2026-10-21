"""Evidence and findings. Every conclusion must point at evidence."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import AwareDatetime, Field

from orqentis.domain.common import DomainModel, Metadata


class EvidenceType(StrEnum):
    PIPELINE_FAILURE = "PIPELINE_FAILURE"
    ACTIVITY_FAILURE = "ACTIVITY_FAILURE"
    # A failed job instance of a notebook, Spark job definition or Dataflow, and the recent runs of an item.
    JOB_FAILURE = "JOB_FAILURE"
    RUN_HISTORY = "RUN_HISTORY"
    ERROR_MESSAGE = "ERROR_MESSAGE"
    SEMANTIC_MODEL_REFRESH = "SEMANTIC_MODEL_REFRESH"
    WORKSPACE_METADATA = "WORKSPACE_METADATA"
    ITEM_METADATA = "ITEM_METADATA"
    CAPACITY_METRIC = "CAPACITY_METRIC"
    DEPENDENCY_EDGE = "DEPENDENCY_EDGE"
    BLAST_RADIUS = "BLAST_RADIUS"
    DEPLOYMENT_COMPARISON = "DEPLOYMENT_COMPARISON"
    HISTORICAL_INCIDENT = "HISTORICAL_INCIDENT"
    RUNBOOK_MATCH = "RUNBOOK_MATCH"
    OPERATIONAL_EVENT = "OPERATIONAL_EVENT"
    SCHEMA_CHANGE = "SCHEMA_CHANGE"
    SLA_STATUS = "SLA_STATUS"
    TOOL_RESULT = "TOOL_RESULT"


class Evidence(DomainModel):
    evidence_id: str
    incident_id: str
    source: str
    resource_id: str | None = None
    resource_type: str | None = None
    collected_at: AwareDatetime
    # When the underlying fact happened (e.g. the failure time), as opposed to collection time.
    observed_at: AwareDatetime | None = None
    evidence_type: EvidenceType
    summary: str
    raw_reference: str | None = None
    structured_payload: dict[str, Any] = Field(default_factory=dict)
    # Reliability of the source: deterministic platform telemetry is 1.0, heuristics lower.
    confidence: float = Field(default=1.0, ge=0, le=1)
    tool_call_id: str | None = None
    # Markers found by the untrusted-content scanner (prompt-injection heuristics).
    untrusted_flags: list[str] = Field(default_factory=list)
    metadata: Metadata = Field(default_factory=dict)


class FindingCategory(StrEnum):
    ROOT_CAUSE = "ROOT_CAUSE"
    CONTRIBUTING_FACTOR = "CONTRIBUTING_FACTOR"
    BLAST_RADIUS = "BLAST_RADIUS"
    SLA_IMPACT = "SLA_IMPACT"
    HISTORICAL_PATTERN = "HISTORICAL_PATTERN"
    CONFIGURATION_DRIFT = "CONFIGURATION_DRIFT"
    CAPACITY_PRESSURE = "CAPACITY_PRESSURE"


class StatementKind(StrEnum):
    FACT = "FACT"
    INFERENCE = "INFERENCE"
    RECOMMENDATION = "RECOMMENDATION"


class Finding(DomainModel):
    finding_id: str
    incident_id: str
    statement: str
    category: FindingCategory
    confidence: float = Field(ge=0, le=1)
    # CRITICAL RULE: a finding must reference supporting evidence.
    evidence_ids: list[str] = Field(min_length=1)
    reasoning_summary: str
    kind: StatementKind = StatementKind.INFERENCE
    produced_by: str
    created_at: AwareDatetime
