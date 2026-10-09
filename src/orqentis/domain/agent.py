"""Auditable record of what the investigator agent did and decided."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import AwareDatetime, Field

from orqentis.domain.common import DomainModel


class ModelUsage(DomainModel):
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


class RejectedAgentOutput(DomainModel):
    kind: str
    content: dict[str, Any]
    reason: str


class AgentDecisionStatus(StrEnum):
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class AgentDecision(DomainModel):
    decision_id: str
    incident_id: str
    trace_id: str
    agent_name: str
    reasoner: str
    model: str | None = None
    prompt_version: str
    input_evidence_ids: list[str] = Field(default_factory=list)
    tool_call_ids: list[str] = Field(default_factory=list)
    accepted_finding_ids: list[str] = Field(default_factory=list)
    root_cause_finding_id: str | None = None
    rejected_outputs: list[RejectedAgentOutput] = Field(default_factory=list)
    insufficient_evidence: bool = False
    summary: str = ""
    usage: ModelUsage | None = None
    status: AgentDecisionStatus = AgentDecisionStatus.COMPLETED
    errors: list[str] = Field(default_factory=list)
    started_at: AwareDatetime
    completed_at: AwareDatetime | None = None
