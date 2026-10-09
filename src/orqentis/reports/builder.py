"""Builds an :class:`IncidentReport` from an investigated incident."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from orqentis.config.clock import Clock
from orqentis.domain.evidence import FindingCategory, StatementKind
from orqentis.domain.incidents import Incident
from orqentis.domain.sla import SLAState
from orqentis.reports.model import IncidentReport, ReportSection, ReportStatement

FACT, INFERENCE, RECOMMENDATION = StatementKind.FACT, StatementKind.INFERENCE, StatementKind.RECOMMENDATION

SECTION_TITLES = (
    "Executive Summary",
    "Observed Symptoms",
    "Probable Root Cause",
    "Confidence",
    "Evidence",
    "Timeline",
    "Blast Radius",
    "Affected Data Products",
    "SLA Impact",
    "Historical Context",
    "Matched Runbook",
    "Recommended Remediation",
    "Risk",
    "Approval Requirement",
    "Validation Plan",
    "Escalation",
)


class ReportBuilder:
    def __init__(self, clock: Clock, *, timezone: str) -> None:
        self._clock = clock
        self._tz = ZoneInfo(timezone)

    def _t(self, value: datetime | None) -> str:
        return value.astimezone(self._tz).strftime("%Y-%m-%d %H:%M %Z") if value else "-"

    def build(self, incident: Incident) -> IncidentReport:
        s: dict[str, list[ReportStatement]] = {title: [] for title in SECTION_TITLES}
        tables: dict[str, list[dict[str, object]]] = {}
        decision = incident.agent_decision

        # Executive summary
        if decision and decision.summary:
            s["Executive Summary"].append(ReportStatement(kind=INFERENCE, text=decision.summary))
        else:
            s["Executive Summary"].append(ReportStatement(kind=FACT, text=f"{incident.title}. Not yet investigated."))

        for symptom in incident.symptoms:
            s["Observed Symptoms"].append(
                ReportStatement(kind=FACT, text=f"{self._t(symptom.observed_at)}: {symptom.description}")
            )

        if incident.probable_root_causes:
            for rc in incident.probable_root_causes:
                s["Probable Root Cause"].append(
                    ReportStatement(
                        kind=INFERENCE, text=f"[{rc.category}] {rc.statement}", evidence_ids=rc.evidence_ids
                    )
                )
        else:
            s["Probable Root Cause"].append(
                ReportStatement(
                    kind=INFERENCE,
                    text="Undetermined. " + (incident.insufficient_evidence_reason or "Insufficient evidence."),
                )
            )
        for finding in incident.findings:
            if finding.category in {
                FindingCategory.CONTRIBUTING_FACTOR,
                FindingCategory.CONFIGURATION_DRIFT,
                FindingCategory.CAPACITY_PRESSURE,
            }:
                s["Probable Root Cause"].append(
                    ReportStatement(
                        kind=finding.kind,
                        text=f"{finding.category.value}: {finding.statement}",
                        evidence_ids=finding.evidence_ids,
                    )
                )

        if incident.confidence:
            c = incident.confidence
            s["Confidence"].append(
                ReportStatement(
                    kind=FACT,
                    text=f"{c.level.value} ({c.score:.2f}), computed deterministically ({c.method})"
                    + (", capped" if c.capped else ""),
                )
            )
            tables["Confidence"] = [
                {
                    "factor": f.name,
                    "weight": f.weight,
                    "value": f.value,
                    "contribution": f.contribution,
                    "explanation": f.explanation,
                }
                for f in c.factors
            ]

        tables["Evidence"] = [
            {
                "id": e.evidence_id.removeprefix(f"{incident.incident_id}-"),
                "type": e.evidence_type.value,
                "source": e.source,
                "resource": e.resource_id or "-",
                "observed": self._t(e.observed_at),
                "summary": e.summary[:160]
                + (" [flags: " + ", ".join(e.untrusted_flags) + "]" if e.untrusted_flags else ""),
            }
            for e in incident.evidence
        ]
        s["Evidence"].append(
            ReportStatement(
                kind=FACT,
                text=f"{len(incident.evidence)} evidence item(s) collected. Raw artefacts are in the evidence store.",
            )
        )

        tables["Timeline"] = [
            {"time": self._t(t.timestamp), "kind": t.kind.value, "actor": t.actor, "event": t.summary}
            for t in sorted(incident.timeline, key=lambda t: t.timestamp)
        ]

        radius = incident.blast_radius
        if radius:
            s["Blast Radius"].append(ReportStatement(kind=FACT, text=radius.summary))
            if radius.cross_workspace:
                s["Blast Radius"].append(ReportStatement(kind=FACT, text="Impact crosses workspace boundaries."))
            tables["Blast Radius"] = [
                {
                    "asset": a.display_name,
                    "type": a.item_type.value,
                    "workspace": a.workspace_id or "-",
                    "impact": a.impact.value,
                    "depth": a.depth,
                    "owner": a.owner or "-",
                }
                for a in radius.assets
            ]
            for asset in radius.assets:
                if asset.item_type.value == "DataProduct":
                    s["Affected Data Products"].append(
                        ReportStatement(kind=FACT, text=f"{asset.display_name} (owner {asset.owner or 'unknown'})")
                    )
        if not s["Affected Data Products"]:
            s["Affected Data Products"].append(ReportStatement(kind=FACT, text="No declared data products affected."))

        for sla in incident.sla_impact:
            kind = FACT if sla.state in {SLAState.MET, SLAState.NOT_APPLICABLE} else INFERENCE
            s["SLA Impact"].append(
                ReportStatement(
                    kind=kind, text=f"{sla.data_product_name}: {sla.state.value}. {sla.description} {sla.reason}."
                )
            )
        if not incident.sla_impact:
            s["SLA Impact"].append(ReportStatement(kind=FACT, text="No SLA applies to the affected assets."))

        for match in incident.related_incidents:
            s["Historical Context"].append(
                ReportStatement(
                    kind=FACT,
                    text=f"{match.incident_id} ({self._t(match.detected_at)}, similarity {match.similarity:.2f}): "
                    f"{match.title}"
                    + (f". Resolution: {match.resolution_summary}" if match.resolution_summary else ""),
                )
            )
        for finding in incident.findings:
            if finding.category is FindingCategory.HISTORICAL_PATTERN:
                s["Historical Context"].append(
                    ReportStatement(kind=INFERENCE, text=finding.statement, evidence_ids=finding.evidence_ids)
                )
        if not s["Historical Context"]:
            s["Historical Context"].append(ReportStatement(kind=FACT, text="No related previous incidents found."))

        recommendation = incident.recommended_actions[0] if incident.recommended_actions else None
        if recommendation and recommendation.runbook_id:
            runbook = next((m for m in incident.matched_runbooks if m.runbook_id == recommendation.runbook_id), None)
            s["Matched Runbook"].append(
                ReportStatement(
                    kind=FACT,
                    text=f"{recommendation.runbook_id} v{recommendation.runbook_version}"
                    + (
                        f" '{runbook.title}' (score {runbook.score:.2f}; {'; '.join(runbook.reasons)})"
                        if runbook
                        else ""
                    ),
                )
            )
        else:
            s["Matched Runbook"].append(ReportStatement(kind=FACT, text="No runbook matched this incident."))

        if recommendation:
            s["Recommended Remediation"].append(
                ReportStatement(
                    kind=RECOMMENDATION, text=recommendation.summary, evidence_ids=recommendation.evidence_ids
                )
            )
            for index, step in enumerate(recommendation.steps, start=1):
                s["Recommended Remediation"].append(ReportStatement(kind=RECOMMENDATION, text=f"{index}. {step}"))
            tables["Recommended Remediation"] = [
                {
                    "action": a.title,
                    "type": a.action_type.value,
                    "target": a.target_display_name or a.target_item_id,
                    "environment": a.environment.value,
                    "risk": a.risk.value if a.risk else "-",
                    "policy": a.policy_decision.effect.value if a.policy_decision else "-",
                    "status": a.status.value,
                    "approval": a.approval_id or "-",
                }
                for a in recommendation.actions
            ]
            s["Risk"].append(
                ReportStatement(
                    kind=FACT,
                    text=f"{recommendation.risk.value} (highest risk of the "
                    "proposed actions, classified by the policy engine)",
                )
            )
            required = sorted(
                {
                    a.policy_decision.required_role
                    for a in recommendation.actions
                    if a.policy_decision and a.policy_decision.required_role
                }
            )
            s["Approval Requirement"].append(
                ReportStatement(
                    kind=FACT,
                    text=("Yes" if recommendation.approval_required else "No")
                    + (
                        f": role {', '.join(required)}; approval state {incident.approval_state.value}"
                        if required
                        else ""
                    )
                    + (f"; approval ids {', '.join(incident.approval_ids)}" if incident.approval_ids else ""),
                )
            )
            for item in recommendation.validation_plan:
                s["Validation Plan"].append(ReportStatement(kind=RECOMMENDATION, text=item))
            for item in recommendation.escalation:
                s["Escalation"].append(ReportStatement(kind=RECOMMENDATION, text=item))
        for title in ("Risk", "Approval Requirement", "Validation Plan", "Escalation", "Recommended Remediation"):
            if not s[title]:
                s[title].append(ReportStatement(kind=FACT, text="Not available (no recommendation)."))

        sections = [ReportSection(title=t, statements=s[t], table=tables.get(t)) for t in SECTION_TITLES]
        return IncidentReport(
            incident_id=incident.incident_id,
            title=incident.title,
            severity=incident.severity.value,
            status=incident.status.value,
            category=incident.category,
            environment=incident.environment.value,
            primary_item=incident.primary_item_name or incident.primary_item_id,
            detected_at=incident.detected_at,
            generated_at=self._clock.now(),
            detected_at_local=self._t(incident.detected_at),
            generated_at_local=self._t(self._clock.now()),
            confidence_level=incident.confidence.level.value if incident.confidence else None,
            confidence_score=incident.confidence.score if incident.confidence else None,
            agent_trace_id=incident.agent_trace_id,
            reasoner=decision.reasoner if decision else None,
            sections=sections,
        )
