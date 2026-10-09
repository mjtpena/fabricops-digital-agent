"""Incident report model."""

from __future__ import annotations

from typing import Any

from pydantic import AwareDatetime, BaseModel, Field

from orqentis.domain.evidence import StatementKind


class ReportStatement(BaseModel):
    kind: StatementKind
    text: str
    evidence_ids: list[str] = Field(default_factory=list)


class ReportSection(BaseModel):
    title: str
    statements: list[ReportStatement] = Field(default_factory=list)
    table: list[dict[str, Any]] | None = None


class IncidentReport(BaseModel):
    incident_id: str
    title: str
    severity: str
    status: str
    category: str
    environment: str
    primary_item: str
    detected_at: AwareDatetime
    generated_at: AwareDatetime
    detected_at_local: str
    generated_at_local: str
    confidence_level: str | None = None
    confidence_score: float | None = None
    agent_trace_id: str | None = None
    reasoner: str | None = None
    sections: list[ReportSection]

    def section(self, title: str) -> ReportSection:
        return next(s for s in self.sections if s.title == title)
