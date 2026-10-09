"""A scripted stand-in for the LLM behind Microsoft Agent Framework.

It plays the model's side of the conversation (tool calls, final answer) so the real
``agent_framework.Agent`` machinery (function invocation, tool schemas, telemetry layers) runs
in tests without a model endpoint.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import Any

from agent_framework import (
    BaseChatClient,
    ChatMiddlewareLayer,
    ChatResponse,
    Content,
    FunctionInvocationLayer,
    Message,
)
from agent_framework.observability import ChatTelemetryLayer

Step = Callable[[list[Message]], ChatResponse]


class ScriptedChatClient(FunctionInvocationLayer, ChatMiddlewareLayer, ChatTelemetryLayer, BaseChatClient):
    def __init__(self, steps: Sequence[Step]) -> None:
        super().__init__()
        self._steps = list(steps)
        self.requests: list[list[Message]] = []
        self.options: list[dict[str, Any]] = []

    async def _inner_get_response(self, *, messages: Any, stream: bool, options: Any, **kwargs: Any) -> ChatResponse:
        self.requests.append(list(messages))
        self.options.append(dict(options))
        if not self._steps:
            raise AssertionError("Scripted model ran out of steps")
        return self._steps.pop(0)(list(messages))


def call_tools(*calls: tuple[str, dict[str, Any]]) -> ChatResponse:
    contents = [
        Content.from_function_call(call_id=f"call-{i}", name=name, arguments=args)
        for i, (name, args) in enumerate(calls)
    ]
    return ChatResponse(messages=[Message(role="assistant", contents=contents)])


def answer(payload: dict[str, Any] | str, *, tokens: tuple[int, int] = (1200, 300)) -> ChatResponse:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return ChatResponse(
        messages=[Message(role="assistant", contents=[text])],
        usage_details={
            "input_token_count": tokens[0],
            "output_token_count": tokens[1],
            "total_token_count": tokens[0] + tokens[1],
        },
    )


def evidence_package(messages: list[Message]) -> dict[str, Any]:
    """Parse the untrusted-data envelope of the first user message."""
    text = next(m.text for m in messages if m.role == "user")
    envelope = json.loads(text[text.index("{") :])
    data: dict[str, Any] = envelope["untrusted_data"]
    return data


def tool_results(messages: list[Message]) -> dict[str, dict[str, Any]]:
    """Map call_id -> parsed tool result envelope (or {'raw': text} for framework errors)."""
    results: dict[str, dict[str, Any]] = {}
    for message in messages:
        for content in message.contents:
            if content.type == "function_result":
                raw = content.result
                try:
                    results[content.call_id] = json.loads(raw) if isinstance(raw, str) else {"raw": raw}
                except json.JSONDecodeError:
                    results[content.call_id] = {"raw": raw}
    return results


def evidence_id_of_type(package: dict[str, Any], evidence_type: str) -> str:
    return next(e["evidence_id"] for e in package["evidence"] if e["evidence_type"] == evidence_type)
