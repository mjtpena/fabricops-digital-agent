"""LLM reasoner built on Microsoft Agent Framework.

The agent receives the evidence package, may call allowlisted read-only tools (and the
approval-only mutation request tools), and must answer with an :class:`AgentDiagnosis` JSON
object. Its output is still subject to grounding validation and deterministic confidence.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from agent_framework import Agent, FunctionTool, SupportsChatGetResponse
from pydantic import ValidationError

from orqentis.agents.models import AgentDiagnosis, EvidencePackage, ReasonerOutput
from orqentis.agents.prompts import system_instructions, user_prompt
from orqentis.agents.toolbox import AgentToolbox
from orqentis.domain.agent import ModelUsage
from orqentis.tools.registry import ToolSpec
from orqentis.tools.untrusted import wrap_for_model

AGENT_NAME = "FabricOpsInvestigator"
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


class AgentOutputError(Exception):
    pass


def parse_diagnosis(text: str) -> AgentDiagnosis:
    cleaned = _FENCE.sub("", text.strip())
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end <= start:
        raise AgentOutputError("Agent response contains no JSON object")
    try:
        return AgentDiagnosis.model_validate_json(cleaned[start : end + 1])
    except ValidationError as exc:
        raise AgentOutputError(f"Agent response does not match the diagnosis schema: {exc}") from exc


def _usage(details: Any, model: str | None) -> ModelUsage | None:
    if not details:
        return None

    def get(key: str) -> int | None:
        value = details.get(key) if isinstance(details, dict) else getattr(details, key, None)
        return int(value) if value is not None else None

    return ModelUsage(
        model=model,
        input_tokens=get("input_token_count"),
        output_tokens=get("output_token_count"),
        total_tokens=get("total_token_count"),
    )


def to_function_tool(spec: ToolSpec, toolbox: AgentToolbox) -> FunctionTool:
    async def invoke(**kwargs: Any) -> str:
        response = await toolbox.invoke(spec.name, kwargs)
        return wrap_for_model(
            {
                "tool_call_id": response.tool_call_id,
                "status": response.status.value,
                "evidence_id": response.metadata.get("evidence_id"),
                "data": response.data,
                "errors": [e.model_dump() for e in response.errors],
            }
        )

    return FunctionTool(name=spec.name, description=spec.description, func=invoke, input_model=spec.input_model)


class AgentFrameworkReasoner:
    name = "agent_framework"

    def __init__(self, client_factory: Callable[[], SupportsChatGetResponse[Any]], *, model: str | None) -> None:
        self._client_factory = client_factory
        self._model = model

    async def diagnose(self, package: EvidencePackage, *, tools: AgentToolbox) -> ReasonerOutput:
        agent = Agent(
            client=self._client_factory(),
            instructions=system_instructions(),
            name=AGENT_NAME,
            description="Evidence-grounded incident investigator for Microsoft Fabric",
            tools=[to_function_tool(spec, tools) for spec in tools.specs()],
        )
        response = await agent.run(user_prompt(package))
        diagnosis = parse_diagnosis(response.text)
        return ReasonerOutput(
            diagnosis=diagnosis,
            reasoner=self.name,
            model=self._model,
            usage=_usage(response.usage_details, self._model),
            tool_calls=tools.calls,
        )


def build_chat_client_factory(
    *,
    provider: str,
    credential: Any,
    azure_openai_endpoint: str | None,
    azure_openai_deployment: str | None,
    azure_openai_api_version: str | None,
    foundry_project_endpoint: str | None,
    foundry_model: str | None,
    max_tool_calls: int,
) -> Callable[[], SupportsChatGetResponse[Any]]:
    """Chat clients authenticate with Microsoft Entra ID (no API keys)."""
    from agent_framework import FunctionInvocationConfiguration

    invocation = FunctionInvocationConfiguration(
        max_iterations=8, max_function_calls=max_tool_calls + 2, include_detailed_errors=False
    )
    if provider == "foundry":

        def foundry() -> SupportsChatGetResponse[Any]:
            from agent_framework.foundry import FoundryChatClient

            client: SupportsChatGetResponse[Any] = FoundryChatClient(
                project_endpoint=foundry_project_endpoint,
                model=foundry_model,
                credential=credential,
                function_invocation_configuration=invocation,
            )
            return client

        return foundry

    def azure_openai() -> SupportsChatGetResponse[Any]:
        try:
            from agent_framework.openai import OpenAIChatClient
        except ModuleNotFoundError as exc:  # pragma: no cover - depends on optional extra
            raise RuntimeError("Install the 'llm' extra: pip install 'orqentis-fabricops[llm]'") from exc
        client: SupportsChatGetResponse[Any] = OpenAIChatClient(
            model=azure_openai_deployment,
            azure_endpoint=azure_openai_endpoint,
            api_version=azure_openai_api_version,
            credential=credential,
            function_invocation_configuration=invocation,
        )
        return client

    return azure_openai
