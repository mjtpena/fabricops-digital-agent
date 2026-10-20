"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from orqentis import __version__
from orqentis.actions.gateway import ActionBlockedError
from orqentis.api.middleware import install_api_hardening
from orqentis.api.routers import approvals, estate, incidents, runbooks, system
from orqentis.approvals.service import (
    ApprovalError,
    ApprovalExpiredError,
    ApprovalForbiddenError,
    ApprovalNotFoundError,
    ApprovalStateError,
)
from orqentis.bootstrap import Container, build_container
from orqentis.config.settings import FabricMode, Settings
from orqentis.incidents.investigation import InvestigationNotPossibleError
from orqentis.incidents.service import IncidentNotFoundError, InvalidTransitionError
from orqentis.observability.logging import get_logger

_log = get_logger(__name__)

_APPROVAL_STATUS = {
    ApprovalNotFoundError: status.HTTP_404_NOT_FOUND,
    ApprovalForbiddenError: status.HTTP_403_FORBIDDEN,
    ApprovalStateError: status.HTTP_409_CONFLICT,
    ApprovalExpiredError: status.HTTP_409_CONFLICT,
}


def _error(code: int, error: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=code, content={"error": error, "message": message})


def create_app(settings: Settings | None = None, *, container: Container | None = None) -> FastAPI:
    settings = settings or (container.settings if container else Settings())

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned = container is None
        app.state.container = container or await build_container(settings)
        if owned and settings.fabric_mode is FabricMode.MOCK:
            _, created = await app.state.container.collect_and_detect()
            _log.info("mock telemetry collected", extra={"incidents_created": [i.incident_id for i in created]})
        try:
            yield
        finally:
            if owned:
                await app.state.container.aclose()

    app = FastAPI(
        title="Orqentis FabricOps API",
        version=__version__,
        description="Internal API of the Orqentis FabricOps digital worker: incidents, evidence, "
        "recommendations, runbooks, estate topology and the approval boundary.",
        lifespan=lifespan,
    )
    app.state.settings = settings
    if container is not None:
        app.state.container = container

    @app.exception_handler(IncidentNotFoundError)
    async def _incident_not_found(_: Request, exc: IncidentNotFoundError) -> JSONResponse:
        return _error(status.HTTP_404_NOT_FOUND, "INCIDENT_NOT_FOUND", f"Incident {exc} not found")

    @app.exception_handler(InvalidTransitionError)
    async def _invalid_transition(_: Request, exc: InvalidTransitionError) -> JSONResponse:
        return _error(status.HTTP_409_CONFLICT, "INVALID_TRANSITION", str(exc))

    @app.exception_handler(InvestigationNotPossibleError)
    async def _not_investigable(_: Request, exc: InvestigationNotPossibleError) -> JSONResponse:
        return _error(status.HTTP_409_CONFLICT, "INVESTIGATION_NOT_POSSIBLE", str(exc))

    @app.exception_handler(ApprovalError)
    async def _approval_error(_: Request, exc: ApprovalError) -> JSONResponse:
        return _error(_APPROVAL_STATUS.get(type(exc), status.HTTP_400_BAD_REQUEST), exc.code, str(exc))

    @app.exception_handler(ActionBlockedError)
    async def _blocked(_: Request, exc: ActionBlockedError) -> JSONResponse:
        return _error(status.HTTP_403_FORBIDDEN, exc.code, exc.message)

    for router in (system.router, incidents.router, estate.router, runbooks.router, approvals.router):
        app.include_router(router)
    install_api_hardening(app, settings)
    return app
