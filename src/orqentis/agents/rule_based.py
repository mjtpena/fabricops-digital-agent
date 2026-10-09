"""Deterministic reasoner: evidence-grounded diagnosis without an LLM.

Used by the local demo and tests, and as the fallback when the LLM reasoner fails. It applies
category-specific hypotheses to the deterministic analysis and cites the evidence behind each.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from orqentis.agents.models import (
    AgentDiagnosis,
    CapacityObservation,
    ErrorObservation,
    EvidencePackage,
    InvestigationContext,
    ProposedFinding,
    ProposedRootCause,
    ReasonerOutput,
)
from orqentis.agents.toolbox import AgentToolbox
from orqentis.domain.evidence import FindingCategory, StatementKind
from orqentis.domain.sla import SLAState
from orqentis.domain.taxonomy import IncidentCategory
from orqentis.incidents.classification import SignatureMatch


def _ids(*values: str | None) -> list[str]:
    return list(dict.fromkeys(v for v in values if v))


class RuleBasedReasoner:
    name = "rule_based"

    def __init__(self, *, timezone: str = "Australia/Sydney") -> None:
        self._tz = ZoneInfo(timezone)

    def _t(self, value: datetime | None) -> str:
        return value.astimezone(self._tz).strftime("%Y-%m-%d %H:%M %Z") if value else "unknown time"

    async def diagnose(self, package: EvidencePackage, *, tools: AgentToolbox) -> ReasonerOutput:
        ctx = package.context
        error = ctx.errors[0] if ctx.errors else None
        signature = ctx.primary_signature
        findings: list[ProposedFinding] = []
        root: ProposedRootCause | None = None

        if error and signature:
            category = signature.category
            if category == IncidentCategory.SCHEMA_DRIFT:
                root = self._schema_drift(ctx, error, signature)
            elif category == IncidentCategory.AUTH_FAILURE:
                root = self._auth(ctx, error, signature)
            elif category in {IncidentCategory.CAPACITY_PRESSURE, IncidentCategory.THROTTLING}:
                root = self._capacity_root(ctx, error, signature)
            else:
                root = ProposedRootCause(
                    statement=f"{signature.description}: {error.activity_name or 'the pipeline'} failed with "
                    f"'{signature.matched_text}'.",
                    category=category,
                    evidence_ids=_ids(error.evidence_id),
                    reasoning_summary=f"Error matches signature {signature.signature_id}; no further corroboration.",
                )

        findings.extend(
            self._capacity_findings(
                ctx,
                package,
                is_root=root is not None
                and root.category in {IncidentCategory.CAPACITY_PRESSURE, IncidentCategory.THROTTLING},
            )
        )
        findings.extend(self._deployment_findings(ctx, root))
        findings.extend(self._history_findings(ctx, root))
        findings.extend(self._impact_findings(ctx))
        findings.extend(self._untrusted_content_findings(package))

        insufficient = None
        if root is None:
            insufficient = (
                "The error does not match any known signature and no correlated upstream change, credential "
                "expiry or capacity pressure was observed. The root cause cannot be determined from the "
                "available telemetry; escalate with the evidence package."
                if error
                else "No failure evidence was collected for this incident."
            )
        runbook_id = None
        if root is not None:
            runbook_id = next((m.runbook_id for m in ctx.runbooks if m.category == root.category), None)

        return ReasonerOutput(
            diagnosis=AgentDiagnosis(
                executive_summary=self._summary(package, root, runbook_id),
                root_cause=root,
                insufficient_evidence_reason=insufficient,
                findings=findings,
                recommended_runbook_id=runbook_id,
            ),
            reasoner=self.name,
        )

    # ------------------------------------------------------------------ root-cause hypotheses
    def _schema_drift(
        self, ctx: InvestigationContext, error: ErrorObservation, signature: SignatureMatch
    ) -> ProposedRootCause:
        column = signature.entities.get("column")
        change = next(
            (
                c
                for c in ctx.schema_changes
                if (column is None or column in c.removed_columns) and c.observed_at <= ctx.failure_time
            ),
            None,
        )
        activity = error.activity_name or "the transformation"
        if change is None:
            return ProposedRootCause(
                statement=f"Schema drift: {activity} expects column '{column or 'unknown'}', which is missing "
                "from its input. No upstream schema-change telemetry was found, so where the change "
                "came from is not confirmed.",
                category=IncidentCategory.SCHEMA_DRIFT,
                evidence_ids=_ids(error.evidence_id),
                reasoning_summary="Error signature only; no corroborating schema-change evidence.",
            )
        rename = (
            f" and '{change.added_columns[0]}' was added, consistent with a rename"
            if len(change.added_columns) == 1 and len(change.removed_columns) == 1
            else ""
        )
        return ProposedRootCause(
            statement=(
                f"Upstream schema change: column '{column or ', '.join(change.removed_columns)}' was removed from "
                f"{change.table or 'the input table'} in {change.item_name} at {self._t(change.observed_at)}"
                f"{rename}. {activity} still expects the original column and failed."
            ),
            category=IncidentCategory.SCHEMA_DRIFT,
            evidence_ids=_ids(error.evidence_id, change.evidence_id, *ctx.upstream_evidence_ids),
            reasoning_summary=(
                f"The missing column named in the error matches the column removed in the schema change on "
                f"{change.item_name}, which is upstream of the failing activity (path "
                f"{' -> '.join(change.upstream_path) or 'n/a'}) and happened before the failure."
            ),
        )

    def _auth(self, ctx: InvestigationContext, error: ErrorObservation, signature: SignatureMatch) -> ProposedRootCause:
        credential = next((c for c in ctx.credentials if c.expired_before_failure), None)
        code = signature.matched_text
        if credential is None:
            return ProposedRootCause(
                statement=f"Authentication to the source failed ({code}); the credential state of the "
                "connection is not observable from the collected evidence.",
                category=IncidentCategory.AUTH_FAILURE,
                evidence_ids=_ids(error.evidence_id),
                reasoning_summary=f"Error matches signature {signature.signature_id}.",
            )
        hours = (ctx.failure_time - credential.expires_at).total_seconds() / 3600
        return ProposedRootCause(
            statement=(
                f"The client secret of service principal '{credential.client_app or signature.entities.get('app')}' "
                f"used by connection {credential.connection_id} expired at {self._t(credential.expires_at)} "
                f"({hours:.0f}h before the failure). {error.activity_name or 'The copy activity'} can no longer "
                f"authenticate to {credential.item_name} ({code})."
            ),
            category=IncidentCategory.AUTH_FAILURE,
            evidence_ids=_ids(error.evidence_id, credential.evidence_id, *ctx.upstream_evidence_ids),
            reasoning_summary="Entra ID error code for an expired secret, and the declared secret expiry of the "
            "upstream source's connection falls before the failure.",
        )

    def _capacity_root(
        self, ctx: InvestigationContext, error: ErrorObservation, signature: SignatureMatch
    ) -> ProposedRootCause:
        cap = ctx.capacity
        if cap is None or not cap.failure_during_throttling:
            return ProposedRootCause(
                statement=f"The workload was rejected by capacity limits ({signature.matched_text}), but no "
                "capacity metrics confirm throttling at the failure time.",
                category=IncidentCategory.CAPACITY_PRESSURE,
                evidence_ids=_ids(error.evidence_id),
                reasoning_summary="Error signature only.",
            )
        return ProposedRootCause(
            statement=(
                f"Capacity {cap.capacity_name}"
                + (f" ({cap.sku})" if cap.sku and cap.sku not in cap.capacity_name else "")
                + f" was saturated when {error.activity_name or 'the job'} "
                f"ran: peak {cap.peak_cu_percent:.0f}% CU with background rejection up to "
                f"{cap.max_background_rejection_percent:.0f}%, so the Spark session was rejected."
            ),
            category=IncidentCategory.CAPACITY_PRESSURE,
            evidence_ids=_ids(error.evidence_id, cap.evidence_id),
            reasoning_summary="Capacity-limit error and capacity metrics show throttling at the failure time.",
        )

    # ------------------------------------------------------------------ supporting findings
    def _capacity_findings(
        self, ctx: InvestigationContext, package: EvidencePackage, *, is_root: bool
    ) -> list[ProposedFinding]:
        cap: CapacityObservation | None = ctx.capacity
        if cap is None:
            return []
        if is_root:
            out: list[ProposedFinding] = []
            for workload in [w for w in cap.contending if not w.in_incident_lineage][:1]:
                where = f"{workload.workspace_name} ({workload.environment.value})" + (
                    ", a non-production workspace on the production capacity"
                    if workload.environment.value != cap.environment.value
                    else ""
                )
                out.append(
                    ProposedFinding(
                        statement=f"Contention: {workload.display_name} in {where} consumed up to "
                        f"{workload.peak_cu_percent:.0f}% of capacity {cap.capacity_name} during the "
                        "failure window.",
                        category=FindingCategory.CAPACITY_PRESSURE,
                        evidence_ids=[cap.evidence_id],
                        reasoning_summary="Largest workload in the capacity metrics during the throttling window and "
                        "not part of the incident's lineage.",
                    )
                )
            return out
        if cap.failure_during_throttling:
            return [
                ProposedFinding(
                    statement=f"Capacity {cap.capacity_name} was throttling at the failure time (peak "
                    f"{cap.peak_cu_percent:.0f}% CU); this may have contributed.",
                    category=FindingCategory.CONTRIBUTING_FACTOR,
                    evidence_ids=[cap.evidence_id],
                    reasoning_summary="Throttling overlaps the failure window.",
                )
            ]
        return [
            ProposedFinding(
                statement=f"Capacity pressure ruled out: {cap.capacity_name} peaked at {cap.peak_cu_percent or 0:.0f}% "
                "CU with no throttling around the failure.",
                category=FindingCategory.CAPACITY_PRESSURE,
                evidence_ids=[cap.evidence_id],
                kind=StatementKind.FACT,
                reasoning_summary="Capacity metrics in the hour before the failure show no throttling.",
            )
        ]

    def _deployment_findings(self, ctx: InvestigationContext, root: ProposedRootCause | None) -> list[ProposedFinding]:
        out: list[ProposedFinding] = []
        for obs in ctx.deployments:
            cmp = obs.comparison
            if cmp.changed_within_window:
                out.append(
                    ProposedFinding(
                        statement=f"{obs.item_name} was deployed to production at "
                        f"{self._t(cmp.last_production_change)}, within {cmp.window_hours}h before the failure. "
                        + (
                            "The change may be related, but no evidence links it to the error."
                            if root is None
                            else "It is a possible contributing change."
                        ),
                        category=FindingCategory.CONTRIBUTING_FACTOR,
                        evidence_ids=[obs.evidence_id],
                        reasoning_summary="Recent production deployment preceding the failure.",
                    )
                )
            elif cmp.drift_detected:
                out.append(
                    ProposedFinding(
                        statement=f"Configuration drift between TEST and PROD for {obs.item_name}: "
                        + "; ".join(cmp.differences),
                        category=FindingCategory.CONFIGURATION_DRIFT,
                        evidence_ids=[obs.evidence_id],
                        reasoning_summary="Deployment comparison across stages.",
                    )
                )
            elif root is not None:
                prod = next((s for s in cmp.states if s.environment.value == "PROD"), None)
                out.append(
                    ProposedFinding(
                        statement=f"No production deployment of {obs.item_name} in the {cmp.window_hours}h before the "
                        f"failure (last {self._t(cmp.last_production_change)}"
                        + (f", commit {prod.commit_id} matches TEST" if prod and len(cmp.states) > 1 else "")
                        + "): a code change on this item is unlikely to be the cause.",
                        category=FindingCategory.CONFIGURATION_DRIFT,
                        evidence_ids=[obs.evidence_id],
                        reasoning_summary="Deployment history rules out a recent change to this item.",
                    )
                )
        return out

    def _history_findings(self, ctx: InvestigationContext, root: ProposedRootCause | None) -> list[ProposedFinding]:
        if ctx.pattern is not None and ctx.pattern_evidence_id:
            contention = ""
            if ctx.capacity and ctx.capacity.failure_during_throttling:
                top = next((w for w in ctx.capacity.contending if not w.in_incident_lineage), None)
                if top:
                    contention = f", correlating with capacity pressure caused by {top.display_name}"
            return [
                ProposedFinding(
                    statement=ctx.pattern.statement + contention + ".",
                    category=FindingCategory.HISTORICAL_PATTERN,
                    evidence_ids=_ids(
                        ctx.pattern_evidence_id, ctx.capacity.evidence_id if contention and ctx.capacity else None
                    ),
                    reasoning_summary=f"Occurrences: {', '.join(ctx.pattern.occurrences)}.",
                )
            ]
        if root is None or not ctx.history_evidence_id:
            return []
        similar = [m for m in ctx.related_incidents if m.category == root.category]
        if not similar:
            return []
        best = similar[0]
        statement = (
            f"Previous occurrence {best.incident_id} ({self._t(best.detected_at)}) has the same failure mode"
            + (
                f" and was resolved with {best.resolved_with_runbook_id}; the same remediation is likely to apply."
                if best.resolved_with_runbook_id
                else "."
            )
        )
        return [
            ProposedFinding(
                statement=statement,
                category=FindingCategory.HISTORICAL_PATTERN,
                evidence_ids=[ctx.history_evidence_id],
                reasoning_summary=f"Similarity {best.similarity:.2f}: {', '.join(best.reasons)}.",
            )
        ]

    def _impact_findings(self, ctx: InvestigationContext) -> list[ProposedFinding]:
        out: list[ProposedFinding] = []
        radius = ctx.blast_radius
        if radius is not None and ctx.blast_radius_evidence_id:
            downstream = [a for a in radius.assets if a.depth > 0]
            if downstream:
                origin_ws = {a.workspace_id for a in radius.assets if a.depth == 0}
                cross = sorted({a.display_name for a in downstream if a.workspace_id not in origin_ws})
                out.append(
                    ProposedFinding(
                        statement=radius.summary + (f". Cross-workspace impact: {', '.join(cross)}" if cross else ""),
                        category=FindingCategory.BLAST_RADIUS,
                        evidence_ids=_ids(ctx.blast_radius_evidence_id, *ctx.downstream_evidence_ids),
                        kind=StatementKind.FACT,
                        reasoning_summary="Downstream traversal of the dependency graph from the failure point(s).",
                    )
                )
        if ctx.sla_evidence_id:
            for sla in ctx.sla:
                if sla.state in {SLAState.AT_RISK, SLAState.BREACHED}:
                    out.append(
                        ProposedFinding(
                            statement=(
                                f"{sla.data_product_name} SLA is {sla.state.value}: {sla.description} {sla.reason}."
                            ),
                            category=FindingCategory.SLA_IMPACT,
                            evidence_ids=[ctx.sla_evidence_id],
                            reasoning_summary="SLA evaluated from freshness events and the open failure.",
                        )
                    )
        return out

    def _untrusted_content_findings(self, package: EvidencePackage) -> list[ProposedFinding]:
        flagged = [e for e in package.evidence if e.untrusted_flags]
        if not flagged:
            return []
        return [
            ProposedFinding(
                statement="Instruction-like content was found in retrieved Fabric metadata and was ignored: "
                + ", ".join(sorted({f for e in flagged for f in e.untrusted_flags}))
                + ".",
                category=FindingCategory.CONTRIBUTING_FACTOR,
                evidence_ids=[e.evidence_id for e in flagged],
                reasoning_summary="Untrusted-content scanner flags on evidence; treated as data only.",
            )
        ]

    def _summary(self, package: EvidencePackage, root: ProposedRootCause | None, runbook_id: str | None) -> str:
        ctx = package.context
        name = package.primary_item.display_name
        when = self._t(ctx.failure_time)
        if root is None:
            head = f"{name} failed at {when}. The root cause could not be determined from the available evidence."
        else:
            head = f"{name} failed at {when}. Probable root cause: {root.statement}"
        parts = [head]
        if ctx.blast_radius is not None:
            downstream = [a for a in ctx.blast_radius.assets if a.depth > 0]
            if downstream:
                parts.append(f"{len(downstream)} downstream asset(s) are affected.")
        risky = [s for s in ctx.sla if s.state in {SLAState.AT_RISK, SLAState.BREACHED}]
        for sla in risky:
            parts.append(f"{sla.data_product_name} SLA is {sla.state.value}.")
        parts.append(f"Runbook {runbook_id} applies." if runbook_id else "No matching runbook; escalate.")
        return " ".join(parts)
