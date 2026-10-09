"""Tool response envelope shared by every tool."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import AwareDatetime, BaseModel, Field


class ToolStatus(StrEnum):
    SUCCESS = "SUCCESS"
    NOT_FOUND = "NOT_FOUND"
    ERROR = "ERROR"
    NOT_ALLOWED = "NOT_ALLOWED"
    ACTION_REQUIRES_APPROVAL = "ACTION_REQUIRES_APPROVAL"
    ACTION_DENIED = "ACTION_DENIED"


class ToolCapability(StrEnum):
    READ = "READ"
    MUTATION_REQUEST = "MUTATION_REQUEST"


class ToolError(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ToolResponse(BaseModel):
    """Every tool returns exactly this shape."""

    tool_call_id: str
    tool_name: str
    timestamp: AwareDatetime
    source: str
    status: ToolStatus
    data: Any = None
    errors: list[ToolError] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ToolCallRecord(BaseModel):
    """Trace entry for one tool invocation (persisted with the investigation trace)."""

    tool_call_id: str
    tool_name: str
    caller: str
    capability: ToolCapability | None = None
    arguments: dict[str, Any]
    status: ToolStatus
    started_at: AwareDatetime
    latency_ms: float
    errors: list[ToolError] = Field(default_factory=list)
    evidence_id: str | None = None
