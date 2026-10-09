"""Reasoner port: anything that turns an evidence package into an :class:`AgentDiagnosis`."""

from __future__ import annotations

from typing import Protocol

from orqentis.agents.models import EvidencePackage, ReasonerOutput
from orqentis.agents.toolbox import AgentToolbox


class Reasoner(Protocol):
    name: str

    async def diagnose(self, package: EvidencePackage, *, tools: AgentToolbox) -> ReasonerOutput: ...
