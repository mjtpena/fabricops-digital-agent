"""FabricOpsInvestigator: runs a reasoner over the evidence package and validates its output."""

from __future__ import annotations

from dataclasses import dataclass

from orqentis.agents.grounding import GroundedDiagnosis, GroundingValidator
from orqentis.agents.models import EvidencePackage, ReasonerOutput
from orqentis.agents.prompts import PROMPT_VERSION
from orqentis.agents.reasoning import Reasoner
from orqentis.agents.toolbox import AgentToolbox
from orqentis.config.clock import Clock
from orqentis.domain.agent import AgentDecision, AgentDecisionStatus
from orqentis.domain.common import new_id
from orqentis.domain.incidents import Incident
from orqentis.domain.principals import AGENT_PRINCIPAL
from orqentis.evidence.recorder import EvidenceRecorder
from orqentis.observability.audit import AuditTrail
from orqentis.observability.logging import get_logger
from orqentis.observability.tracing import get_tracer
from orqentis.tools.models import ToolCallRecord
from orqentis.tools.registry import ToolContext, ToolRegistry

_log = get_logger(__name__)
AGENT_NAME = "FabricOpsInvestigator"


@dataclass
class InvestigatorResult:
    grounded: GroundedDiagnosis
    decision: AgentDecision
    tool_calls: list[ToolCallRecord]


class FabricOpsInvestigator:
    def __init__(
        self,
        reasoner: Reasoner,
        registry: ToolRegistry,
        *,
        tool_allowlist: frozenset[str],
        audit: AuditTrail,
        clock: Clock,
        max_tool_calls: int,
        fallback: Reasoner | None = None,
    ) -> None:
        self._reasoner = reasoner
        self._fallback = fallback
        self._registry = registry
        self._allowlist = tool_allowlist
        self._audit = audit
        self._clock = clock
        self._max_tool_calls = max_tool_calls
        self._grounding = GroundingValidator(clock)

    @property
    def reasoner_name(self) -> str:
        return self._reasoner.name

    async def investigate(
        self, package: EvidencePackage, *, incident: Incident, recorder: EvidenceRecorder, trace_id: str
    ) -> InvestigatorResult:
        started = self._clock.now()
        ctx = ToolContext(
            principal=AGENT_PRINCIPAL,
            trace_id=trace_id,
            incident=incident,
            allowed_tools=self._allowlist,
            recorder=recorder,
            auto_record_evidence=True,
            max_calls=self._max_tool_calls,
        )
        toolbox = AgentToolbox(self._registry, ctx)
        errors: list[str] = []
        status = AgentDecisionStatus.COMPLETED
        reasoner_name = self._reasoner.name
        with get_tracer().start_as_current_span("agent.invoke") as span:
            span.set_attribute("orqentis.agent", AGENT_NAME)
            span.set_attribute("orqentis.reasoner", reasoner_name)
            span.set_attribute("orqentis.incident_id", incident.incident_id)
            try:
                output: ReasonerOutput = await self._reasoner.diagnose(package, tools=toolbox)
            except Exception as exc:
                if self._fallback is None:
                    raise
                errors.append(f"{reasoner_name} failed: {type(exc).__name__}: {exc}")
                status = AgentDecisionStatus.FAILED
                _log.warning("reasoner failed; using fallback", extra={"reasoner": reasoner_name, "error": str(exc)})
                output = await self._fallback.diagnose(package, tools=toolbox)
                reasoner_name = f"{self._fallback.name} (fallback after {self._reasoner.name} failure)"
            if output.usage and output.usage.total_tokens is not None:
                span.set_attribute("gen_ai.usage.total_tokens", output.usage.total_tokens)

        grounded = self._grounding.validate(
            output.diagnosis, incident=incident, runbooks=package.context.runbooks, produced_by=reasoner_name
        )
        decision = AgentDecision(
            decision_id=new_id("DEC"),
            incident_id=incident.incident_id,
            trace_id=trace_id,
            agent_name=AGENT_NAME,
            reasoner=reasoner_name,
            model=output.model,
            prompt_version=PROMPT_VERSION,
            input_evidence_ids=[e.evidence_id for e in package.evidence],
            tool_call_ids=[c.tool_call_id for c in toolbox.calls],
            accepted_finding_ids=[f.finding_id for f in grounded.findings],
            root_cause_finding_id=grounded.root_cause.finding_id if grounded.root_cause else None,
            rejected_outputs=grounded.rejected,
            insufficient_evidence=grounded.root_cause is None,
            summary=grounded.summary,
            usage=output.usage,
            status=status,
            errors=errors + output.errors,
            started_at=started,
            completed_at=self._clock.now(),
        )
        await self._audit.record(
            actor=AGENT_PRINCIPAL.id,
            action="agent.decision",
            outcome=status.value,
            incident_id=incident.incident_id,
            trace_id=trace_id,
            details={
                "reasoner": reasoner_name,
                "model": output.model,
                "prompt_version": PROMPT_VERSION,
                "accepted_findings": len(grounded.findings),
                "rejected_outputs": len(grounded.rejected),
                "tool_calls": len(toolbox.calls),
                "usage": output.usage.model_dump() if output.usage else None,
            },
        )
        return InvestigatorResult(grounded=grounded, decision=decision, tool_calls=toolbox.calls)
