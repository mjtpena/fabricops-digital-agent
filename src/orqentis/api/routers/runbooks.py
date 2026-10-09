"""Runbook endpoints."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from orqentis.api.auth import CurrentPrincipal
from orqentis.api.deps import ContainerDep
from orqentis.api.schemas import RunbookSummary
from orqentis.domain.runbooks import Runbook

router = APIRouter(prefix="/runbooks", tags=["runbooks"])


@router.get("", response_model=list[RunbookSummary])
async def list_runbooks(c: ContainerDep, _: CurrentPrincipal) -> list[RunbookSummary]:
    return [
        RunbookSummary(
            id=r.id,
            version=r.version,
            title=r.title,
            category=r.category,
            risk=r.risk.value,
            approval_required=r.approval_required,
            owner=r.owner,
            last_reviewed=r.last_reviewed.isoformat(),
            path=r.path,
        )
        for r in c.runbooks.all()
    ]


@router.get("/{runbook_id}", response_model=Runbook)
async def get_runbook(runbook_id: str, c: ContainerDep, _: CurrentPrincipal) -> Runbook:
    runbook = c.runbooks.get(runbook_id)
    if runbook is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Runbook {runbook_id} not found")
    return runbook
