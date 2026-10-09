"""Workflow port and the investigation trace record."""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Protocol

from pydantic import AwareDatetime, BaseModel, Field

from orqentis.config.clock import Clock
from orqentis.domain.actions import PolicyDecision
from orqentis.domain.agent import AgentDecision
from orqentis.domain.incidents import Incident
from orqentis.observability.tracing import get_tracer
from orqentis.tools.models import ToolCallRecord


class StepRecord(BaseModel):
    number: int
    name: str
    started_at: AwareDatetime
    latency_ms: float
    status: str = "OK"
    note: str | None = None


class InvestigationTrace(BaseModel):
    trace_id: str
    incident_id: str
    workflow: str
    started_at: AwareDatetime
    completed_at: AwareDatetime | None = None
    steps: list[StepRecord] = Field(default_factory=list)
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    agent_decision: AgentDecision | None = None
    policy_decisions: list[PolicyDecision] = Field(default_factory=list)
    approval_ids: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)

    @contextmanager
    def step(self, number: int, name: str, clock: Clock) -> Iterator[StepRecord]:
        record = StepRecord(number=number, name=name, started_at=clock.now(), latency_ms=0.0)
        t0 = time.perf_counter()
        with get_tracer().start_as_current_span(f"investigation.step.{number:02d}") as span:
            span.set_attribute("orqentis.step", name)
            try:
                yield record
            except Exception as exc:
                record.status = "FAILED"
                record.note = f"{type(exc).__name__}: {exc}"
                raise
            finally:
                record.latency_ms = round((time.perf_counter() - t0) * 1000, 2)
                self.steps.append(record)


class InvestigationWorkflow(Protocol):
    name: str

    def supports(self, incident: Incident) -> bool: ...

    async def run(self, incident: Incident, *, trace: InvestigationTrace) -> Incident: ...
