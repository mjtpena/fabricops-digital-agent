"""Approval endpoints. Humans decide; execution is a separate, explicitly gated step."""

from __future__ import annotations

from fastapi import APIRouter, Query

from orqentis.actions.gateway import ActionOutcome
from orqentis.api.authz import DecidingPrincipal, ReadPrincipal
from orqentis.api.deps import ContainerDep
from orqentis.api.schemas import DecisionRequest
from orqentis.domain.approvals import ApprovalRequest, ApprovalStatus

router = APIRouter(prefix="/approvals", tags=["approvals"])


@router.get("", response_model=list[ApprovalRequest])
async def list_approvals(
    c: ContainerDep,
    _: ReadPrincipal,
    incident_id: str | None = None,
    status: ApprovalStatus | None = Query(default=None),
) -> list[ApprovalRequest]:
    return await c.approvals.list(incident_id=incident_id, status=status)


@router.get("/{approval_id}", response_model=ApprovalRequest)
async def get_approval(approval_id: str, c: ContainerDep, _: ReadPrincipal) -> ApprovalRequest:
    return await c.approvals.get(approval_id)


@router.post("/{approval_id}/approve", response_model=ApprovalRequest)
async def approve(
    approval_id: str, c: ContainerDep, principal: DecidingPrincipal, body: DecisionRequest | None = None
) -> ApprovalRequest:
    return await c.approvals.decide(
        approval_id, principal=principal, approve=True, comment=body.comment if body else None
    )


@router.post("/{approval_id}/reject", response_model=ApprovalRequest)
async def reject(
    approval_id: str, c: ContainerDep, principal: DecidingPrincipal, body: DecisionRequest | None = None
) -> ApprovalRequest:
    return await c.approvals.decide(
        approval_id, principal=principal, approve=False, comment=body.comment if body else None
    )


@router.post("/{approval_id}/execute", response_model=ActionOutcome)
async def execute(approval_id: str, c: ContainerDep, principal: DecidingPrincipal) -> ActionOutcome:
    """Execute an APPROVED action. Refused unless the write capability is enabled for this deployment."""
    return await c.gateway.execute(approval_id, principal=principal)
