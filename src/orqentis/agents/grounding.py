"""Grounding validation: reasoner output is accepted only where it is backed by evidence."""

from __future__ import annotations

from dataclasses import dataclass, field

from orqentis.agents.models import AgentDiagnosis
from orqentis.config.clock import Clock
from orqentis.domain.agent import RejectedAgentOutput
from orqentis.domain.evidence import Finding, FindingCategory, StatementKind
from orqentis.domain.incidents import Incident
from orqentis.domain.runbooks import RunbookMatch
from orqentis.domain.taxonomy import TAXONOMY
from orqentis.tools.untrusted import sanitize_text

MAX_FINDINGS = 15


@dataclass
class GroundedDiagnosis:
    findings: list[Finding]
    root_cause: Finding | None
    root_cause_category: str | None
    insufficient_evidence_reason: str | None
    runbook_id: str | None
    summary: str
    rejected: list[RejectedAgentOutput] = field(default_factory=list)


class GroundingValidator:
    def __init__(self, clock: Clock) -> None:
        self._clock = clock

    def validate(
        self, diagnosis: AgentDiagnosis, *, incident: Incident, runbooks: list[RunbookMatch], produced_by: str
    ) -> GroundedDiagnosis:
        evidence = incident.evidence_by_id()
        rejected: list[RejectedAgentOutput] = []
        findings: list[Finding] = []
        now = self._clock.now()

        def check(ids: list[str]) -> str | None:
            if not ids:
                return "no supporting evidence cited"
            unknown = [i for i in ids if i not in evidence]
            if unknown:
                return f"cites evidence that does not exist: {', '.join(unknown)}"
            return None

        def confidence_of(ids: list[str]) -> float:
            return round(sum(evidence[i].confidence for i in ids) / len(ids), 3)

        def next_id() -> str:
            return f"{incident.incident_id}-F{len(findings) + 1:02d}"

        root_finding: Finding | None = None
        root_category: str | None = None
        insufficient = diagnosis.insufficient_evidence_reason
        if diagnosis.root_cause is not None:
            rc = diagnosis.root_cause
            problem = check(rc.evidence_ids)
            category = rc.category.strip().upper()
            if problem is None and not TAXONOMY.is_known(category):
                problem = f"unknown taxonomy category {rc.category!r}"
            if problem:
                rejected.append(RejectedAgentOutput(kind="root_cause", content=rc.model_dump(), reason=problem))
                insufficient = insufficient or f"Proposed root cause rejected: {problem}"
            else:
                root_finding = Finding(
                    finding_id=next_id(),
                    incident_id=incident.incident_id,
                    statement=sanitize_text(rc.statement, max_length=1200),
                    category=FindingCategory.ROOT_CAUSE,
                    confidence=confidence_of(rc.evidence_ids),
                    evidence_ids=list(dict.fromkeys(rc.evidence_ids)),
                    reasoning_summary=sanitize_text(rc.reasoning_summary, max_length=1200),
                    kind=StatementKind.INFERENCE,
                    produced_by=produced_by,
                    created_at=now,
                )
                findings.append(root_finding)
                root_category = category
                insufficient = None

        for proposed in diagnosis.findings[:MAX_FINDINGS]:
            if proposed.category is FindingCategory.ROOT_CAUSE:
                rejected.append(
                    RejectedAgentOutput(
                        kind="finding",
                        content=proposed.model_dump(mode="json"),
                        reason="root cause must be provided via 'root_cause'",
                    )
                )
                continue
            problem = check(proposed.evidence_ids)
            if problem:
                rejected.append(
                    RejectedAgentOutput(kind="finding", content=proposed.model_dump(mode="json"), reason=problem)
                )
                continue
            findings.append(
                Finding(
                    finding_id=next_id(),
                    incident_id=incident.incident_id,
                    statement=sanitize_text(proposed.statement, max_length=1200),
                    category=proposed.category,
                    confidence=confidence_of(proposed.evidence_ids),
                    evidence_ids=list(dict.fromkeys(proposed.evidence_ids)),
                    reasoning_summary=sanitize_text(proposed.reasoning_summary, max_length=1200),
                    kind=proposed.kind,
                    produced_by=produced_by,
                    created_at=now,
                )
            )

        runbook_id = diagnosis.recommended_runbook_id
        if runbook_id and runbook_id not in {m.runbook_id for m in runbooks}:
            rejected.append(
                RejectedAgentOutput(
                    kind="runbook",
                    content={"recommended_runbook_id": runbook_id},
                    reason="runbook was not retrieved from the runbook catalog",
                )
            )
            runbook_id = None
        if runbook_id is None and root_category is not None:
            runbook_id = next((m.runbook_id for m in runbooks if m.category == root_category), None)

        if root_finding is None and not insufficient:
            insufficient = "No root cause supported by evidence was proposed."
        return GroundedDiagnosis(
            findings=findings,
            root_cause=root_finding,
            root_cause_category=root_category,
            insufficient_evidence_reason=insufficient,
            runbook_id=runbook_id,
            summary=sanitize_text(diagnosis.executive_summary, max_length=2000),
            rejected=rejected,
        )
