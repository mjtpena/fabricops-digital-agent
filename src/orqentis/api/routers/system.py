"""Health, telemetry collection and audit endpoints."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Query, Request, Response, status
from fastapi.responses import JSONResponse

from orqentis import __version__
from orqentis.api.auth import CurrentPrincipal
from orqentis.api.deps import ContainerDep
from orqentis.api.schemas import CollectionResult, Health, NotReady, Ready
from orqentis.config.settings import Settings
from orqentis.observability.audit import AuditRecord, AuditVerification
from orqentis.runtime.service import Runtime

router = APIRouter(tags=["system"])


@router.get("/health", response_model=Health)
async def health(request: Request) -> Health:
    """Liveness. Cheap and dependency-free: it answers 200 while the container is still being built or failing."""
    settings: Settings = request.app.state.settings
    container = request.app.state.runtime.container
    return Health(
        status="ok",
        version=__version__,
        fabric_mode=settings.fabric_mode.value,
        reasoner=container.investigator.reasoner_name if container else settings.reasoner.value,
        write_actions_enabled=settings.enable_write_actions,
        time=container.clock.now() if container else datetime.now(UTC),
    )


@router.get("/health/ready", response_model=Ready, responses={503: {"model": NotReady}})
async def ready(request: Request, response: Response) -> Ready | JSONResponse:
    """Readiness: 200 once the container is built and the estate catalog has been loaded, else 503 with a reason."""
    runtime: Runtime = request.app.state.runtime
    container = runtime.container
    if container is None:
        build = runtime.status
        body = NotReady(
            reason="build_failed" if build.reason == "build_failed" else "starting",
            error_type=build.error_type,
            attempts=build.attempts,
            retry_in_seconds=build.retry_in_seconds,
        )
        return JSONResponse(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, content=body.model_dump())
    settings: Settings = request.app.state.settings
    if not settings.scheduler_enabled:
        return Ready(scheduler="disabled")
    return Ready(scheduler="leader" if container.scheduler.is_leader else "standby")


@router.post("/telemetry/collect", response_model=CollectionResult)
async def collect(c: ContainerDep, _: CurrentPrincipal) -> CollectionResult:
    ingestion, created = await c.collect_and_detect()
    return CollectionResult(
        new_events=len(ingestion.new_events),
        by_type=ingestion.counts(),
        incidents_created=[i.incident_id for i in created],
        collector_errors=ingestion.errors,
    )


@router.get("/audit", response_model=list[AuditRecord])
async def audit(
    c: ContainerDep, _: CurrentPrincipal, incident_id: str | None = None, limit: int = Query(default=200, ge=1, le=5000)
) -> list[AuditRecord]:
    return await c.audit.list(incident_id=incident_id, limit=limit)


@router.get("/audit/verify", response_model=AuditVerification)
async def verify_audit(c: ContainerDep, _: CurrentPrincipal) -> AuditVerification:
    return await c.audit.verify()
