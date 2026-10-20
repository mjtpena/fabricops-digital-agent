"""Health, telemetry collection and audit endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Query

from orqentis import __version__
from orqentis.api.authz import CollectPrincipal, ReadPrincipal, VerifyPrincipal
from orqentis.api.deps import ContainerDep
from orqentis.api.schemas import CollectionResult, Health
from orqentis.observability.audit import AuditRecord, AuditVerification

router = APIRouter(tags=["system"])


@router.get("/health", response_model=Health)
async def health(c: ContainerDep) -> Health:
    return Health(
        status="ok",
        version=__version__,
        fabric_mode=c.settings.fabric_mode.value,
        reasoner=c.investigator.reasoner_name,
        write_actions_enabled=c.settings.enable_write_actions,
        time=c.clock.now(),
    )


@router.post("/telemetry/collect", response_model=CollectionResult)
async def collect(c: ContainerDep, _: CollectPrincipal) -> CollectionResult:
    ingestion, created = await c.collect_and_detect()
    return CollectionResult(
        new_events=len(ingestion.new_events),
        by_type=ingestion.counts(),
        incidents_created=[i.incident_id for i in created],
        collector_errors=ingestion.errors,
    )


@router.get("/audit", response_model=list[AuditRecord])
async def audit(
    c: ContainerDep, _: ReadPrincipal, incident_id: str | None = None, limit: int = Query(default=200, ge=1, le=5000)
) -> list[AuditRecord]:
    return await c.audit.list(incident_id=incident_id, limit=limit)


@router.get("/audit/verify", response_model=AuditVerification)
async def verify_audit(c: ContainerDep, _: VerifyPrincipal) -> AuditVerification:
    return await c.audit.verify()
