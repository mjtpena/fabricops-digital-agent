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
    SlaBreachObservation,
)
from orqentis.agents.toolbox import AgentToolbox
from orqentis.domain.common import ItemType
from orqentis.domain.evidence import FindingCategory, StatementKind
from orqentis.domain.sla import SLAState
from orqentis.domain.taxonomy import IncidentCategory
from orqentis.incidents.classification import SignatureMatch
from orqentis.telemetry.delta import clip


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

        if ctx.sla_breach is not None:
            root = self._sla_root(package, ctx.sla_breach)
        elif error and signature:
            category = signature.category
            if category == IncidentCategory.SCHEMA_DRIFT:
                root = self._schema_drift(ctx, error, signature)
            elif category == IncidentCategory.AUTH_FAILURE:
                root = self._auth(ctx, error, signature)
            elif category in {IncidentCategory.CAPACITY_PRESSURE, IncidentCategory.THROTTLING}:
                root = self._capacity_root(ctx, error, signature)
            elif category == IncidentCategory.SEMANTIC_MODEL_REFRESH_FAILURE:
                root = self._refresh_root(package, error, signature)
            elif signature.signature_id == "SIG-NOTEBOOK-EXCEPTION":
                root = self._exception_root(package, error, signature)
            else:
                root = ProposedRootCause(
                    statement=f"{signature.description}: {self._subject(package, error)} failed with "
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
        findings.extend(self._run_history_findings(package, root))
        findings.extend(self._upstream_incident_findings(ctx, root))
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
                if (
                    column is None
                    or column in c.removed_columns
                    or column in [before for before, _ in c.renamed_columns]
                )
                and c.observed_at <= ctx.failure_time
            ),
            None,
        )
        activity = error.activity_name or "the transformation"
        if change is None:
            hint = self._suggestion(signature)
            return ProposedRootCause(
                statement=f"Schema drift: {activity} expects column '{column or 'a column'}', which is missing "
                f"from its input{hint}. No upstream schema-change telemetry was found, so where the change "
                "came from is not confirmed.",
                category=IncidentCategory.SCHEMA_DRIFT,
                evidence_ids=_ids(error.evidence_id),
                reasoning_summary="Error signature only; no corroborating schema-change evidence.",
            )
        renamed_to = dict(change.renamed_columns).get(column or "")
        if renamed_to is not None:
            rename = f" and renamed to '{renamed_to}' (Delta column mapping)"
        elif len(change.added_columns) == 1 and len(change.removed_columns) == 1:
            rename = f" and '{change.added_columns[0]}' was added, consistent with a rename"
        else:
            rename = ""
        return ProposedRootCause(
            statement=(
                f"Upstream schema change: column '{column or ', '.join(change.removed_columns) or '?'}' was removed "
                f"from {change.table or 'the input table'} in {change.item_name} at {self._t(change.observed_at)}"
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

    def _subject(self, package: EvidencePackage, error: ErrorObservation) -> str:
        """What failed, in words: the activity that raised the error, else the item itself."""
        if error.activity_name:
            return error.activity_name
        return (
            "the pipeline"
            if package.primary_item.item_type is ItemType.DATA_PIPELINE
            else package.primary_item.display_name
        )

    @staticmethod
    def _suggestion(signature: SignatureMatch) -> str:
        suggestions = signature.entities.get("suggestions")
        return f" (Spark suggests {clip(suggestions, 120)})" if suggestions else ""

    def _exception_root(
        self, package: EvidencePackage, error: ErrorObservation, signature: SignatureMatch
    ) -> ProposedRootCause:
        ctx = package.context
        exception = clip(signature.entities.get("exception", "an exception"), 80)
        message = signature.entities.get("exception_message")
        ruled_out = [
            what
            for what, absent in (
                ("an upstream schema change", not ctx.schema_changes),
                ("an expired credential", not ctx.credentials),
                ("capacity pressure", ctx.capacity is not None and not ctx.capacity.throttling_detected),
            )
            if absent
        ]
        return ProposedRootCause(
            statement=(
                f"{self._subject(package, error)} raised {exception}"
                + (f": {clip(message, 200)}" if message else "")
                + ". The exception comes from the job's own code or data; the cause inside it is not determined."
            ),
            category=IncidentCategory.ACTIVITY_FAILURE,
            evidence_ids=_ids(error.evidence_id, ctx.run_history.evidence_id if ctx.run_history else None),
            reasoning_summary=(
                f"Error matches signature {signature.signature_id}: a Python or Spark exception surfaced by the run."
                + (f" Nothing observed points to {', '.join(ruled_out)}." if ruled_out else "")
            ),
        )

    def _refresh_root(
        self, package: EvidencePackage, error: ErrorObservation, signature: SignatureMatch
    ) -> ProposedRootCause:
        ctx = package.context
        table = signature.entities.get("table")
        model_table = signature.entities.get("model_table")
        name = package.primary_item.display_name
        cause = (
            f"it references the source table '{clip(table, 128)}'"
            + (f" (model table '{clip(model_table, 128)}')" if model_table else "")
            + ", which the engine cannot access: the table does not exist, or the identity has no permission on it"
            if table
            else f"the engine reported {signature.matched_text}"
        )
        return ProposedRootCause(
            statement=f"The refresh of {name} failed: {cause}.",
            category=IncidentCategory.SEMANTIC_MODEL_REFRESH_FAILURE,
            evidence_ids=_ids(error.evidence_id, ctx.run_history.evidence_id if ctx.run_history else None),
            reasoning_summary=f"Error matches signature {signature.signature_id}: a refresh error from the engine.",
        )

    @staticmethod
    def _duration(seconds: float | None) -> str:
        if seconds is None:
            return "unknown"
        minutes = round(seconds / 60)
        return f"{minutes // 60} h {minutes % 60} min" if minutes >= 60 else f"{minutes} min"

    def _sla_root(self, package: EvidencePackage, b: SlaBreachObservation) -> ProposedRootCause:
        ctx = package.context
        item = package.primary_item.display_name
        head = f"{b.data_product_name} missed its {self._t(b.deadline)} deadline"
        blockers = ctx.upstream_incidents
        if b.kind == "LATE":
            detail = ""
            if b.started_at is not None:
                detail = f" The run started at {self._t(b.started_at)}"
                detail += f" (usually around {b.usual_start_local})" if b.usual_start_local else ""
                if b.duration_seconds is not None:
                    detail += f" and took {self._duration(b.duration_seconds)}"
                    detail += (
                        f" (median {self._duration(b.median_duration_seconds)})" if b.median_duration_seconds else ""
                    )
                detail += "."
            statement = (
                f"{head}: {item} finished refreshing at {self._t(b.last_successful_update)}, "
                f"{b.minutes_late or 0:.0f} min late.{detail}"
            )
            reasoning = (
                "The last successful run ended after the deadline; "
                "its start and duration are compared with earlier runs."
            )
        elif b.kind == "BLOCKED":
            listing = "; ".join(f"{u.incident_id} ({u.category}) on {u.item_name}" for u in blockers)
            statement = f"{head} because {item} could not refresh today: {listing} blocked the refresh."
            reasoning = "An incident on the item, or on something upstream of it, was open before the deadline."
        elif b.kind == "FAILED":
            statement = f"{head}: the last run of {item} failed and no incident is open for it."
            reasoning = "The most recent run of the freshness item failed; nothing blocks it any more."
        else:
            statement = (
                f"{head}: no completed run of {item} was observed in the window. It did not start, or is still running."
            )
            reasoning = "No run of the freshness item finished since the start of the SLA window."
        return ProposedRootCause(
            statement=statement,
            category=IncidentCategory.SLA_BREACH,
            evidence_ids=_ids(b.evidence_id, b.run_evidence_id, *[u.evidence_id for u in blockers]),
            reasoning_summary=reasoning,
        )

    # ------------------------------------------------------------------ supporting findings
    def _run_history_findings(self, package: EvidencePackage, root: ProposedRootCause | None) -> list[ProposedFinding]:
        history = package.context.run_history
        if history is None or package.context.sla_breach is not None:
            return []
        name = package.primary_item.display_name
        if history.consecutive_failures == 0:
            return []
        since = f" since {self._t(history.first_failure_at)}" if history.first_failure_at else ""
        last = (
            f"; the last success was at {self._t(history.last_success_at)}"
            if history.last_success_at
            else "; no success in the recent runs"
        )
        return [
            ProposedFinding(
                statement=f"{name} has failed {history.consecutive_failures} time(s) in a row{since}{last}.",
                category=FindingCategory.HISTORICAL_PATTERN,
                evidence_ids=[history.evidence_id],
                kind=StatementKind.FACT,
                reasoning_summary=f"Recent runs of the item ({history.runs_considered} considered), newest first.",
            )
        ]

    def _upstream_incident_findings(
        self, ctx: InvestigationContext, root: ProposedRootCause | None
    ) -> list[ProposedFinding]:
        if not ctx.upstream_incidents or (ctx.sla_breach is not None and ctx.sla_breach.kind == "BLOCKED"):
            return []
        listing = "; ".join(
            f"{u.incident_id} ({u.category}, {u.status}) on {u.item_name}" for u in ctx.upstream_incidents
        )
        return [
            ProposedFinding(
                statement=f"An incident was already open at or upstream of this item: {listing}. "
                "It may have caused this failure.",
                category=FindingCategory.CONTRIBUTING_FACTOR,
                evidence_ids=[u.evidence_id for u in ctx.upstream_incidents],
                reasoning_summary="Open incidents on the item or on items upstream of it in the dependency graph.",
            )
        ]

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
        what = (
            f"{ctx.sla_breach.data_product_name} missed its SLA"
            if ctx.sla_breach
            else f"{name} refresh failed"
            if package.primary_item.item_type is ItemType.SEMANTIC_MODEL
            else f"{name} failed"
        )
        if root is None:
            head = f"{what} at {when}. The root cause could not be determined from the available evidence."
        else:
            head = f"{what} at {when}. Probable root cause: {root.statement}"
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
