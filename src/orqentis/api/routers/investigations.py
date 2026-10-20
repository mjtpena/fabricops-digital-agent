"""Investigation jobs: poll the outcome of ``POST /incidents/{id}/investigate``."""

from __future__ import annotations

from fastapi import APIRouter

from orqentis.api.authz import ReadPrincipal
from orqentis.api.deps import ContainerDep
from orqentis.runtime.investigations import InvestigationJob, InvestigationJobNotFoundError

router = APIRouter(prefix="/investigations", tags=["investigations"])


@router.get("/{job_id}", response_model=InvestigationJob)
async def get_investigation(job_id: str, c: ContainerDep, _: ReadPrincipal) -> InvestigationJob:
    """Status (QUEUED, RUNNING, SUCCEEDED, FAILED), timestamps and the result summary of one job."""
    job = c.investigation_jobs.get(job_id)
    if job is None:
        raise InvestigationJobNotFoundError(job_id)
    return job
