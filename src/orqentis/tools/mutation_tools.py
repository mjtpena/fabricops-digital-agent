"""Mutation-request tools.

These are the only tools that refer to changes. They never change anything: they hand a proposed
action to the ActionGateway, which evaluates policy and opens an approval request. The response is
``ACTION_REQUIRES_APPROVAL`` (or ``ACTION_DENIED``), never an executed change.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from orqentis.actions.gateway import ActionGateway, GatewayStatus
from orqentis.config.clock import Clock
from orqentis.domain.actions import Action, ActionType
from orqentis.domain.common import new_id
from orqentis.tools.models import ToolCapability, ToolStatus
from orqentis.tools.registry import ToolContext, ToolOutcome, ToolSpec
from orqentis.topology.catalog import EstateCatalog

_ID = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
_REASON = Field(min_length=5, max_length=1000)


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PipelineRerunRequest(_Input):
    pipeline_id: str = _ID
    reason: str = _REASON


class SemanticModelRefreshRequest(_Input):
    semantic_model_id: str = _ID
    reason: str = _REASON


class ItemChangeRequest(_Input):
    action_type: ActionType
    target_item_id: str = _ID
    parameters: dict[str, Any] = Field(default_factory=dict)
    reason: str = _REASON

    @field_validator("action_type")
    @classmethod
    def _must_be_mutation(cls, value: ActionType) -> ActionType:
        if not value.is_mutation:
            raise ValueError("Use read-only tools for read-only operations")
        return value


def build_mutation_request_tools(gateway: ActionGateway, catalog: EstateCatalog, clock: Clock) -> list[ToolSpec]:
    async def _submit(
        ctx: ToolContext,
        action_type: ActionType,
        target_item_id: str,
        reason: str,
        parameters: dict[str, Any],
        *,
        idempotent: bool,
    ) -> ToolOutcome:
        incident = ctx.incident
        if incident is None:
            raise ValueError("Mutation requests must be made in the context of an incident")
        node = catalog.node(target_item_id)
        if node is None:
            raise LookupError(f"Item {target_item_id} is not in the estate catalog")
        action = Action(
            action_id=new_id("ACT"),
            incident_id=incident.incident_id,
            action_type=action_type,
            title=f"{action_type.value} {node.display_name}",
            description=reason,
            target_item_id=target_item_id,
            target_display_name=node.display_name,
            workspace_id=node.workspace_id,
            environment=node.environment,
            parameters=parameters,
            idempotent=idempotent,
            requested_by=ctx.principal.id,
            created_at=clock.now(),
        )
        outcome = await gateway.submit(action, principal=ctx.principal, incident=incident)
        if outcome.approval is not None and outcome.approval.approval_id not in incident.approval_ids:
            incident.approval_ids.append(outcome.approval.approval_id)
        status = {
            GatewayStatus.ACTION_REQUIRES_APPROVAL: ToolStatus.ACTION_REQUIRES_APPROVAL,
            GatewayStatus.ACTION_DENIED: ToolStatus.ACTION_DENIED,
        }.get(outcome.status, ToolStatus.ERROR)
        return ToolOutcome(
            data={
                "executed": False,
                "action_id": action.action_id,
                "action_type": action_type.value,
                "target_item_id": target_item_id,
                "policy_effect": outcome.decision.effect.value,
                "risk": outcome.decision.risk.value,
                "required_role": outcome.decision.required_role,
                "approval_id": outcome.approval.approval_id if outcome.approval else None,
                "message": outcome.message,
            },
            source="orqentis.action-gateway",
            status=status,
            resource_id=target_item_id,
            summary=outcome.message,
        )

    async def request_pipeline_rerun(p: PipelineRerunRequest, ctx: ToolContext) -> ToolOutcome:
        return await _submit(ctx, ActionType.RERUN_PIPELINE, p.pipeline_id, p.reason, {}, idempotent=True)

    async def request_semantic_model_refresh(p: SemanticModelRefreshRequest, ctx: ToolContext) -> ToolOutcome:
        return await _submit(ctx, ActionType.REFRESH_SEMANTIC_MODEL, p.semantic_model_id, p.reason, {}, idempotent=True)

    async def request_item_change(p: ItemChangeRequest, ctx: ToolContext) -> ToolOutcome:
        return await _submit(ctx, p.action_type, p.target_item_id, p.reason, p.parameters, idempotent=False)

    return [
        ToolSpec(
            "request_pipeline_rerun",
            "Request a pipeline rerun. Never executes: returns ACTION_REQUIRES_APPROVAL with an approval id.",
            PipelineRerunRequest,
            request_pipeline_rerun,
            capability=ToolCapability.MUTATION_REQUEST,
        ),
        ToolSpec(
            "request_semantic_model_refresh",
            "Request a semantic model refresh. Never executes: returns ACTION_REQUIRES_APPROVAL.",
            SemanticModelRefreshRequest,
            request_semantic_model_refresh,
            capability=ToolCapability.MUTATION_REQUEST,
        ),
        ToolSpec(
            "request_item_change",
            "Request any other change to an item. Never executes: returns ACTION_REQUIRES_APPROVAL or "
            "ACTION_DENIED according to policy.",
            ItemChangeRequest,
            request_item_change,
            capability=ToolCapability.MUTATION_REQUEST,
        ),
    ]
