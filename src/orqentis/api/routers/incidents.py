"""Incident endpoints."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Query, Response

from orqentis.api.auth import CurrentPrincipal
from orqentis.api.deps import ContainerDep
from orqentis.api.schemas import IncidentSummary, InvestigationResult
from orqentis.domain.actions import Recommendation
from orqentis.domain.evidence import Evidence
from orqentis.domain.incidents import Incident, IncidentStatus
from orqentis.reports.markdown import render_markdown
from orqentis.reports.model import IncidentReport
from orqentis.storage.incidents import IncidentQuery

router = APIRouter(prefix="/incidents", tags=["incidents"])


@router.get("", response_model=list[IncidentSummary])
async def list_incidents(
    c: ContainerDep,
    _: CurrentPrincipal,
    status: list[IncidentStatus] | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
) -> list[IncidentSummary]:
    incidents = await c.incidents.repository.list(IncidentQuery(statuses=status, limit=limit))
    return [IncidentSummary.of(i) for i in incidents]


@router.get("/{incident_id}", response_model=Incident)
async def get_incident(incident_id: str, c: ContainerDep, _: CurrentPrincipal) -> Incident:
    return await c.incidents.get(incident_id)


@router.post("/{incident_id}/investigate", response_model=InvestigationResult)
async def investigate(incident_id: str, c: ContainerDep, principal: CurrentPrincipal) -> InvestigationResult:
    incident, trace = await c.investigations.investigate(incident_id, actor=principal)
    return InvestigationResult(
        incident=IncidentSummary.of(incident),
        trace_id=trace.trace_id,
        findings=len(incident.findings),
        evidence=len(incident.evidence),
        tool_calls=len(trace.tool_calls),
        approval_ids=incident.approval_ids,
    )


@router.get("/{incident_id}/evidence", response_model=list[Evidence])
async def get_evidence(incident_id: str, c: ContainerDep, _: CurrentPrincipal) -> list[Evidence]:
    return (await c.incidents.get(incident_id)).evidence


@router.get("/{incident_id}/recommendations", response_model=list[Recommendation])
async def get_recommendations(incident_id: str, c: ContainerDep, _: CurrentPrincipal) -> list[Recommendation]:
    return (await c.incidents.get(incident_id)).recommended_actions


@router.get("/{incident_id}/report", response_model=IncidentReport, responses={200: {"content": {"text/markdown": {}}}})
async def get_report(
    incident_id: str,
    c: ContainerDep,
    _: CurrentPrincipal,
    format: Literal["json", "markdown"] = "json",
) -> IncidentReport | Response:
    report = c.reports.build(await c.incidents.get(incident_id))
    if format == "markdown":
        return Response(render_markdown(report), media_type="text/markdown")
    return report
