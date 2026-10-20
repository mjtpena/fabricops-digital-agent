"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from orqentis import __version__
from orqentis.actions.gateway import ActionBlockedError, AuditUnavailableError
from orqentis.api.deps import NotReadyError
from orqentis.api.middleware import install_api_hardening
from orqentis.api.routers import approvals, estate, incidents, investigations, runbooks, system
from orqentis.approvals.service import (
    ApprovalError,
    ApprovalExpiredError,
    ApprovalForbiddenError,
    ApprovalNotFoundError,
    ApprovalStateError,
)
from orqentis.bootstrap import Container, build_container
from orqentis.config.settings import Settings
from orqentis.incidents.investigation import InvestigationNotPossibleError
from orqentis.incidents.service import IncidentNotFoundError, InvalidTransitionError
from orqentis.runtime.investigations import InvestigationJobNotFoundError
from orqentis.runtime.service import Runtime
from orqentis.ui.install import install_cross_origin_guard, install_ui

_APPROVAL_STATUS = {
    ApprovalNotFoundError: status.HTTP_404_NOT_FOUND,
    ApprovalForbiddenError: status.HTTP_403_FORBIDDEN,
    ApprovalStateError: status.HTTP_409_CONFLICT,
    ApprovalExpiredError: status.HTTP_409_CONFLICT,
}


def _error(code: int, error: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=code, content={"error": error, "message": message})


def create_app(
    settings: Settings | None = None,
    *,
    container: Container | None = None,
    container_factory: Callable[[Settings], Awaitable[Container]] | None = None,
) -> FastAPI:
    """Build the API.

    Without ``container`` the app builds its own in the background (retrying with backoff if that fails) so that
    ``/health`` answers at once and ``/health/ready`` reports when the estate is loaded. ``container_factory`` lets
    tests and embedders replace how the container is built (default: ``build_container``, looked up when the app is
    created).
    """
    settings = settings or (container.settings if container else Settings())
    runtime = Runtime(settings, container_factory or build_container, container=container)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await runtime.start()
        try:
            yield
        finally:
            await runtime.stop()

    app = FastAPI(
        title="Orqentis FabricOps API",
        version=__version__,
        description="Internal API of the Orqentis FabricOps digital worker: incidents, evidence, "
        "recommendations, runbooks, estate topology and the approval boundary.",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.runtime = runtime

    @app.exception_handler(IncidentNotFoundError)
    async def _incident_not_found(_: Request, exc: IncidentNotFoundError) -> JSONResponse:
        return _error(status.HTTP_404_NOT_FOUND, "INCIDENT_NOT_FOUND", f"Incident {exc} not found")

    @app.exception_handler(NotReadyError)
    async def _not_ready(_: Request, __: NotReadyError) -> JSONResponse:
        response = _error(
            status.HTTP_503_SERVICE_UNAVAILABLE, "NOT_READY", "The service is starting; see /health/ready"
        )
        response.headers["Retry-After"] = "5"
        return response

    @app.exception_handler(InvestigationJobNotFoundError)
    async def _job_not_found(_: Request, exc: InvestigationJobNotFoundError) -> JSONResponse:
        return _error(status.HTTP_404_NOT_FOUND, "INVESTIGATION_NOT_FOUND", f"No investigation found for {exc}")

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

    @app.exception_handler(AuditUnavailableError)
    async def _audit_unavailable(_: Request, exc: AuditUnavailableError) -> JSONResponse:
        return _error(status.HTTP_503_SERVICE_UNAVAILABLE, exc.code, exc.message)

    for router in (
        system.router,
        incidents.router,
        investigations.router,
        estate.router,
        runbooks.router,
        approvals.router,
    ):
        app.include_router(router)
    # Added first, so it sits inside the body size limit: an oversized body is still answered 413 before anything else.
    install_cross_origin_guard(app, settings)
    install_api_hardening(app, settings)
    if settings.ui_enabled:
        install_ui(app, settings)  # after the handlers above: it wraps them to answer /ui with HTML pages
    return app
