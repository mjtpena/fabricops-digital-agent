"""Incident endpoints."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Query, Response

from orqentis.api.authz import InvestigatePrincipal, ReadPrincipal
from orqentis.api.deps import ContainerDep
from orqentis.api.schemas import IncidentSummary, InvestigationAccepted
from orqentis.domain.actions import Recommendation
from orqentis.domain.evidence import Evidence
from orqentis.domain.incidents import Incident, IncidentStatus
from orqentis.reports.markdown import render_markdown
from orqentis.reports.model import IncidentReport
from orqentis.runtime.investigations import InvestigationJob, InvestigationJobNotFoundError, InvestigationTrigger
from orqentis.storage.incidents import IncidentQuery

router = APIRouter(prefix="/incidents", tags=["incidents"])


@router.get("", response_model=list[IncidentSummary])
async def list_incidents(
    c: ContainerDep,
    _: ReadPrincipal,
    status: list[IncidentStatus] | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
) -> list[IncidentSummary]:
    incidents = await c.incidents.repository.list(IncidentQuery(statuses=status, limit=limit))
    return [IncidentSummary.of(i) for i in incidents]


@router.get("/{incident_id}", response_model=Incident)
async def get_incident(incident_id: str, c: ContainerDep, _: ReadPrincipal) -> Incident:
    return await c.incidents.get(incident_id)


@router.post("/{incident_id}/investigate", response_model=InvestigationAccepted, status_code=202)
async def investigate(
    incident_id: str, c: ContainerDep, principal: InvestigatePrincipal, response: Response
) -> InvestigationAccepted:
    """Queue an investigation and return immediately; poll ``GET /investigations/{job_id}`` for the outcome.

    Idempotent per incident: while one is queued or running, the same job is returned (``already_active``).
    """
    submission = await c.investigation_jobs.submit(
        incident_id, requested_by=principal, trigger=InvestigationTrigger.API
    )
    status_url = f"/investigations/{submission.job.job_id}"
    response.headers["Location"] = status_url
    return InvestigationAccepted(
        **submission.job.model_dump(), status_url=status_url, already_active=not submission.created
    )


@router.get("/{incident_id}/investigation", response_model=InvestigationJob)
async def latest_investigation(incident_id: str, c: ContainerDep, _: ReadPrincipal) -> InvestigationJob:
    """The most recent investigation job of an incident (this process keeps the last few hundred jobs)."""
    await c.incidents.get(incident_id)  # 404 for an unknown incident
    job = c.investigation_jobs.latest_for(incident_id)
    if job is None:
        raise InvestigationJobNotFoundError(incident_id)
    return job


@router.get("/{incident_id}/evidence", response_model=list[Evidence])
async def get_evidence(incident_id: str, c: ContainerDep, _: ReadPrincipal) -> list[Evidence]:
    return (await c.incidents.get(incident_id)).evidence


@router.get("/{incident_id}/recommendations", response_model=list[Recommendation])
async def get_recommendations(incident_id: str, c: ContainerDep, _: ReadPrincipal) -> list[Recommendation]:
    return (await c.incidents.get(incident_id)).recommended_actions


@router.get("/{incident_id}/report", response_model=IncidentReport, responses={200: {"content": {"text/markdown": {}}}})
async def get_report(
    incident_id: str,
    c: ContainerDep,
    _: ReadPrincipal,
    format: Literal["json", "markdown"] = "json",
) -> IncidentReport | Response:
    report = c.reports.build(await c.incidents.get(incident_id))
    if format == "markdown":
        return Response(render_markdown(report), media_type="text/markdown")
    return report
