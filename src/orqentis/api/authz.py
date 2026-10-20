"""Route-level authorisation: which kind of caller, holding which role, may use which endpoint.

Layers (outermost first):

1. ``current_principal`` classifies the caller (HUMAN or SERVICE) from validated token claims.
2. These dependencies gate each route class. ``DecidingPrincipal`` is unconditional: only a HUMAN may
   approve, reject or execute, and a token carrying the application-only ``FabricOps.Automation`` role never
   may, whatever its classification. Role checks for reads, collection and investigations are on by default
   under Easy Auth (``Settings.read_role_required``).
3. The approval service and the action gateway re-check kind, requester and role. These checks are the
   source of truth. The route-level ones are defence in depth and also leave an audit record.
"""

from __future__ import annotations

from typing import Annotated, NoReturn

from fastapi import Depends, Request, status

from orqentis.api.auth import CurrentPrincipal
from orqentis.api.errors import ApiError
from orqentis.api.roles import COLLECT_ROLES, INVESTIGATE_ROLES, READ_ROLES, ROLE_AUTOMATION
from orqentis.config.settings import Settings
from orqentis.domain.principals import Principal, PrincipalKind


async def _refuse(request: Request, principal: Principal, code: str, message: str, *, audit: bool) -> NoReturn:
    container = getattr(request.app.state, "container", None)
    if audit and container is not None:
        await container.audit.record(
            actor=principal.id,
            action="api.authorize",
            outcome="REFUSED",
            details={
                "code": code,
                "method": request.method,
                "path": request.url.path,
                "kind": principal.kind.value,
                "roles": sorted(principal.roles),
            },
        )
    raise ApiError(status.HTTP_403_FORBIDDEN, code, message)


async def _require_any_role(
    request: Request, principal: Principal, allowed: frozenset[str], *, audit: bool
) -> Principal:
    settings: Settings = request.app.state.settings
    if settings.read_role_required and not principal.roles & allowed:
        await _refuse(
            request,
            principal,
            "ROLE_REQUIRED",
            f"One of the roles {', '.join(sorted(allowed))} is required for this endpoint",
            audit=audit,
        )
    return principal


async def require_read(request: Request, principal: CurrentPrincipal) -> Principal:
    return await _require_any_role(request, principal, READ_ROLES, audit=False)


async def require_collect(request: Request, principal: CurrentPrincipal) -> Principal:
    return await _require_any_role(request, principal, COLLECT_ROLES, audit=True)


async def require_investigate(request: Request, principal: CurrentPrincipal) -> Principal:
    return await _require_any_role(request, principal, INVESTIGATE_ROLES, audit=True)


async def require_decider(request: Request, principal: CurrentPrincipal) -> Principal:
    """Approve, reject and execute are human-only. Not configurable and independent of the read-role switch."""
    if principal.kind is not PrincipalKind.HUMAN:
        await _refuse(
            request,
            principal,
            "PRINCIPAL_NOT_HUMAN",
            "Only a human principal can approve, reject or execute (applications and agents cannot)",
            audit=True,
        )
    if ROLE_AUTOMATION in principal.roles:
        await _refuse(
            request,
            principal,
            "AUTOMATION_ROLE_CANNOT_DECIDE",
            f"A token carrying {ROLE_AUTOMATION} is an application token and cannot approve, reject or execute",
            audit=True,
        )
    return principal


ReadPrincipal = Annotated[Principal, Depends(require_read)]
CollectPrincipal = Annotated[Principal, Depends(require_collect)]
InvestigatePrincipal = Annotated[Principal, Depends(require_investigate)]
DecidingPrincipal = Annotated[Principal, Depends(require_decider)]
