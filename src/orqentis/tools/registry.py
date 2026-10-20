"""Tool registry: the single tool boundary for the deterministic workflow and the LLM agent."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ValidationError

from orqentis.config.clock import Clock
from orqentis.domain.common import new_id
from orqentis.domain.evidence import EvidenceType
from orqentis.domain.incidents import Incident
from orqentis.domain.principals import Principal
from orqentis.fabric.errors import FabricCapabilityUnavailableError, FabricError, FabricNotFoundError
from orqentis.observability.audit import AuditTrail
from orqentis.observability.logging import get_logger
from orqentis.observability.metrics import DISABLED, UNKNOWN, Metrics
from orqentis.observability.redaction import redact
from orqentis.observability.tracing import get_tracer
from orqentis.tools.models import ToolCallRecord, ToolCapability, ToolError, ToolResponse, ToolStatus

if TYPE_CHECKING:
    from orqentis.evidence.recorder import EvidenceRecorder

_log = get_logger(__name__)


@dataclass
class ToolOutcome:
    """What a handler returns; the registry wraps it into a :class:`ToolResponse`."""

    data: Any
    source: str
    summary: str
    status: ToolStatus = ToolStatus.SUCCESS
    resource_id: str | None = None
    resource_type: str | None = None
    observed_at: datetime | None = None
    errors: list[ToolError] = field(default_factory=list)


ToolHandler = Callable[[Any, "ToolContext"], Awaitable[ToolOutcome]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_model: type[BaseModel]
    handler: ToolHandler
    capability: ToolCapability = ToolCapability.READ
    evidence_type: EvidenceType = EvidenceType.TOOL_RESULT
    timeout_seconds: float = 30.0


@dataclass
class ToolContext:
    principal: Principal
    trace_id: str
    incident: Incident | None = None
    # ``None`` = trusted internal caller (deterministic workflow); a set = enforced allowlist (agent).
    allowed_tools: frozenset[str] | None = None
    recorder: EvidenceRecorder | None = None
    auto_record_evidence: bool = False
    # Hard cap on tool calls made through this context (agent budget); ``None`` = unlimited.
    max_calls: int | None = None
    call_log: list[ToolCallRecord] = field(default_factory=list)
    # Calls admitted so far; reserved synchronously so concurrent calls cannot overshoot ``max_calls``.
    admitted: int = 0


def to_jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {k: to_jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [to_jsonable(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    return value


class ToolRegistry:
    def __init__(
        self, specs: Sequence[ToolSpec], audit: AuditTrail, clock: Clock, *, metrics: Metrics = DISABLED
    ) -> None:
        self._specs = {s.name: s for s in specs}
        if len(self._specs) != len(specs):
            raise ValueError("Duplicate tool names")
        self._audit = audit
        self._clock = clock
        self._metrics = metrics

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    def specs(self, *, allowed: frozenset[str] | None = None) -> list[ToolSpec]:
        return [s for name, s in sorted(self._specs.items()) if allowed is None or name in allowed]

    async def invoke(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResponse:
        call_id = new_id("TC")
        started = self._clock.now()
        t0 = time.perf_counter()
        spec = self._specs.get(name)
        outcome: ToolOutcome | None = None
        errors: list[ToolError] = []
        status: ToolStatus
        tracer = get_tracer()
        with tracer.start_as_current_span(f"tool.{name}") as span:
            span.set_attribute("orqentis.tool.name", name)
            span.set_attribute("orqentis.tool.caller", ctx.principal.id)
            if ctx.incident is not None:
                span.set_attribute("orqentis.incident_id", ctx.incident.incident_id)
            over_budget = ctx.max_calls is not None and ctx.admitted >= ctx.max_calls
            if not over_budget:
                ctx.admitted += 1
            if over_budget:
                status = ToolStatus.NOT_ALLOWED
                errors.append(
                    ToolError(code="TOOL_BUDGET_EXHAUSTED", message=f"Tool call budget of {ctx.max_calls} exhausted")
                )
            elif ctx.allowed_tools is not None and name not in ctx.allowed_tools:
                status = ToolStatus.NOT_ALLOWED
                errors.append(
                    ToolError(
                        code="TOOL_NOT_ALLOWED", message=f"Tool '{name}' is not on the allowlist for {ctx.principal.id}"
                    )
                )
            elif spec is None:
                status = ToolStatus.NOT_ALLOWED
                errors.append(ToolError(code="UNKNOWN_TOOL", message=f"Tool '{name}' does not exist"))
            else:
                try:
                    params = spec.input_model.model_validate(arguments)
                except ValidationError as exc:
                    status = ToolStatus.ERROR
                    errors.append(
                        ToolError(
                            code="INVALID_ARGUMENTS",
                            message="Argument validation failed",
                            details={"errors": exc.errors(include_url=False, include_context=False)},
                        )
                    )
                else:
                    status, outcome = await self._run(spec, params, ctx, errors)
            latency_ms = round((time.perf_counter() - t0) * 1000, 2)
            span.set_attribute("orqentis.tool.status", status.value)
            span.set_attribute("orqentis.tool.latency_ms", latency_ms)
        # A name the model made up must not become a metric attribute: only registered tools are labelled by name.
        self._metrics.tool_call(name if spec is not None else UNKNOWN, status)

        response = ToolResponse(
            tool_call_id=call_id,
            tool_name=name,
            timestamp=started,
            source=outcome.source if outcome else "orqentis.tools",
            status=status,
            data=redact(to_jsonable(outcome.data)) if outcome else None,
            errors=errors,
            metadata={
                "latency_ms": latency_ms,
                "trace_id": ctx.trace_id,
                "caller": ctx.principal.id,
                "incident_id": ctx.incident.incident_id if ctx.incident else None,
                "summary": outcome.summary if outcome else None,
            },
        )
        evidence_id: str | None = None
        if (
            ctx.recorder is not None
            and ctx.auto_record_evidence
            and outcome is not None
            and spec is not None
            and status is ToolStatus.SUCCESS
            and spec.capability is ToolCapability.READ
        ):
            evidence = await ctx.recorder.record_tool_response(response, spec.evidence_type, outcome)
            evidence_id = evidence.evidence_id
            response.metadata["evidence_id"] = evidence_id

        record = ToolCallRecord(
            tool_call_id=call_id,
            tool_name=name,
            caller=ctx.principal.id,
            capability=spec.capability if spec else None,
            arguments=redact(arguments),
            status=status,
            started_at=started,
            latency_ms=latency_ms,
            errors=errors,
            evidence_id=evidence_id,
        )
        ctx.call_log.append(record)
        _log.info(
            "tool invoked",
            extra={
                "tool": name,
                "tool_call_id": call_id,
                "status": status.value,
                "latency_ms": latency_ms,
                "caller": ctx.principal.id,
            },
        )
        await self._audit.record(
            actor=ctx.principal.id,
            action="tool.invoke",
            outcome=status.value,
            incident_id=ctx.incident.incident_id if ctx.incident else None,
            trace_id=ctx.trace_id,
            details={
                "tool": name,
                "tool_call_id": call_id,
                "arguments": arguments,
                "latency_ms": latency_ms,
                "errors": [e.code for e in errors],
            },
        )
        return response

    async def _run(
        self, spec: ToolSpec, params: BaseModel, ctx: ToolContext, errors: list[ToolError]
    ) -> tuple[ToolStatus, ToolOutcome | None]:
        try:
            outcome = await asyncio.wait_for(spec.handler(params, ctx), timeout=spec.timeout_seconds)
        except FabricNotFoundError as exc:
            errors.append(ToolError(code=exc.error_code or "NOT_FOUND", message=exc.message, details=exc.to_dict()))
            return ToolStatus.NOT_FOUND, None
        except FabricCapabilityUnavailableError as exc:
            errors.append(ToolError(code="CAPABILITY_UNAVAILABLE", message=exc.message, details=exc.to_dict()))
            return ToolStatus.ERROR, None
        except FabricError as exc:
            errors.append(ToolError(code=exc.error_code or "FABRIC_ERROR", message=exc.message, details=exc.to_dict()))
            return ToolStatus.ERROR, None
        except TimeoutError:
            errors.append(ToolError(code="TIMEOUT", message=f"Tool timed out after {spec.timeout_seconds}s"))
            return ToolStatus.ERROR, None
        except LookupError as exc:
            errors.append(ToolError(code="NOT_FOUND", message=str(exc)))
            return ToolStatus.NOT_FOUND, None
        except ValueError as exc:
            errors.append(ToolError(code="INVALID_REQUEST", message=str(exc)))
            return ToolStatus.ERROR, None
        errors.extend(outcome.errors)
        return outcome.status, outcome
