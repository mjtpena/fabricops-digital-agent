"""Version-controlled runbooks and their retrieval matches."""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Any

from pydantic import Field

from orqentis.domain.actions import ActionType, RiskLevel
from orqentis.domain.common import DomainModel, ItemType
from orqentis.domain.taxonomy import CategoryName, Severity

REQUIRED_RUNBOOK_SECTIONS: tuple[str, ...] = (
    "Symptoms",
    "Diagnostic Steps",
    "Possible Causes",
    "Recommended Remediation",
    "Validation",
    "Rollback",
    "Escalation",
)


class ActionTarget(StrEnum):
    """Which incident-relative item a runbook action applies to."""

    PRIMARY_ITEM = "primary_item"
    FAILURE_POINT = "failure_point"
    AFFECTED_SEMANTIC_MODELS = "affected_semantic_models"
    UPSTREAM_SOURCE = "upstream_source"
    CONTENDING_WORKLOAD = "contending_workload"
    CAPACITY = "capacity"


class RunbookActionTemplate(DomainModel):
    action_type: ActionType
    title: str
    target: ActionTarget = ActionTarget.PRIMARY_ITEM
    idempotent: bool = False
    parameters: dict[str, Any] = Field(default_factory=dict)


class Runbook(DomainModel):
    id: str
    version: str
    title: str
    category: CategoryName
    severity: Severity
    signals: list[str] = Field(min_length=1)
    applies_to: list[ItemType] = Field(min_length=1)
    risk: RiskLevel
    approval_required: bool
    owner: str
    last_reviewed: date
    remediation_actions: list[RunbookActionTemplate] = Field(default_factory=list)
    sections: dict[str, str]
    path: str
    content_hash: str

    def section_steps(self, name: str) -> list[str]:
        """Numbered / bulleted lines of a section, without list markers."""
        steps: list[str] = []
        for raw in self.sections.get(name, "").splitlines():
            line = raw.strip()
            if not line:
                continue
            for marker in ("- ", "* "):
                if line.startswith(marker):
                    line = line[len(marker) :]
                    break
            else:
                head, _, tail = line.partition(". ")
                if head.isdigit() and tail:
                    line = tail
            steps.append(line)
        return steps


class RunbookMatch(DomainModel):
    runbook_id: str
    version: str
    title: str
    category: str
    score: float = Field(ge=0, le=1)
    matched_signals: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    path: str
    content_hash: str
    risk: RiskLevel
    approval_required: bool
