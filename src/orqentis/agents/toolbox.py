"""The agent's view of the tool registry: allowlisted, budgeted, evidence-recording."""

from __future__ import annotations

from typing import Any

from orqentis.tools.models import ToolCallRecord, ToolResponse
from orqentis.tools.registry import ToolContext, ToolRegistry, ToolSpec


class AgentToolbox:
    def __init__(self, registry: ToolRegistry, context: ToolContext) -> None:
        self._registry = registry
        self._context = context

    def specs(self) -> list[ToolSpec]:
        return self._registry.specs(allowed=self._context.allowed_tools)

    async def invoke(self, name: str, arguments: dict[str, Any]) -> ToolResponse:
        return await self._registry.invoke(name, arguments, self._context)

    @property
    def calls(self) -> list[ToolCallRecord]:
        return list(self._context.call_log)
