"""Deterministic confidence assessment (never an LLM-chosen number)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from orqentis.domain.common import DomainModel


class ConfidenceLevel(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class ConfidenceFactor(DomainModel):
    name: str
    weight: float = Field(ge=0, le=1)
    value: float = Field(ge=0, le=1)
    contribution: float = Field(ge=0, le=1)
    explanation: str
    evidence_ids: list[str] = Field(default_factory=list)


class ConfidenceAssessment(DomainModel):
    score: float = Field(ge=0, le=1)
    level: ConfidenceLevel
    factors: list[ConfidenceFactor]
    method: str
    capped: bool = False
