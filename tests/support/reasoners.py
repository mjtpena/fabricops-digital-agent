"""Reasoners for tests that need to control when an investigation finishes."""

from __future__ import annotations

import asyncio

from orqentis.agents.models import EvidencePackage, ReasonerOutput
from orqentis.agents.rule_based import RuleBasedReasoner
from orqentis.agents.toolbox import AgentToolbox


class GatedReasoner:
    """The rule-based reasoner, but it waits until the test lets it go: a stand-in for a slow LLM."""

    name = "gated"

    def __init__(self) -> None:
        self._inner = RuleBasedReasoner(timezone="Australia/Sydney")
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def diagnose(self, package: EvidencePackage, *, tools: AgentToolbox) -> ReasonerOutput:
        self.started.set()
        await self.release.wait()
        return await self._inner.diagnose(package, tools=tools)


class HangingReasoner:
    """Never answers (the instance running it is killed while it waits)."""

    name = "hanging"

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def diagnose(self, package: EvidencePackage, *, tools: AgentToolbox) -> ReasonerOutput:
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")
