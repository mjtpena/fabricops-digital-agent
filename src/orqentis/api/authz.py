"""Route-level authorisation: which kind of caller, holding which role, may use which endpoint.

Layers (outermost first):

1. ``current_principal`` classifies the caller (HUMAN or SERVICE) from validated token claims.
2. These dependencies gate each route class. First the caller's per-principal rate budget is charged (keyed on
   the authenticated principal id, never on a raw header), then the role is checked.
   * ``DecidingPrincipal`` (approve, reject, execute) is unconditional: only a HUMAN may decide, and a token
     carrying the application-only ``FabricOps.Automation`` role never may, whatever its classification.
   * Collection, investigation and audit verification need their roles whenever ``Settings.action_roles_required``
     holds, which is always the case under Easy Auth.
   * Plain reads need any FabricOps role while ``Settings.read_role_required`` holds (the default under Easy Auth).
3. The approval service and the action gateway re-check kind, requester and role. These checks are the
   source of truth. The route-level ones are defence in depth and also leave an audit record.
"""

from __future__ import annotations

from typing import Annotated, NoReturn

from fastapi import Depends, Request, status

from orqentis.api.auth import CurrentPrincipal
from orqentis.api.errors import ApiError
from orqentis.api.middleware import RateLimits, RequestClass
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


def _charge(request: Request, principal: Principal, request_class: RequestClass) -> None:
    """Spend one token of the authenticated principal's budget (429 when it is empty)."""
    limits: RateLimits | None = getattr(request.app.state, "rate_limits", None)
    if limits is not None:
        limits.check(request_class, principal.id)


async def _require_any_role(
    request: Request, principal: Principal, allowed: frozenset[str], *, enforced: bool, audit: bool
) -> Principal:
    if enforced and not principal.roles & allowed:
        await _refuse(
            request,
            principal,
            "ROLE_REQUIRED",
            f"One of the roles {', '.join(sorted(allowed))} is required for this endpoint",
            audit=audit,
        )
    return principal


async def require_read(request: Request, principal: CurrentPrincipal) -> Principal:
    settings: Settings = request.app.state.settings
    return await _require_any_role(request, principal, READ_ROLES, enforced=settings.read_role_required, audit=False)


async def require_verify(request: Request, principal: CurrentPrincipal) -> Principal:
    """``GET /audit/verify`` walks the whole hash chain: a read that is limited like an expensive call."""
    _charge(request, principal, "expensive")
    return await require_read(request, principal)


async def require_collect(request: Request, principal: CurrentPrincipal) -> Principal:
    _charge(request, principal, "expensive")
    settings: Settings = request.app.state.settings
    return await _require_any_role(
        request, principal, COLLECT_ROLES, enforced=settings.action_roles_required, audit=True
    )


async def require_investigate(request: Request, principal: CurrentPrincipal) -> Principal:
    _charge(request, principal, "expensive")
    settings: Settings = request.app.state.settings
    return await _require_any_role(
        request, principal, INVESTIGATE_ROLES, enforced=settings.action_roles_required, audit=True
    )


async def require_decider(request: Request, principal: CurrentPrincipal) -> Principal:
    """Approve, reject and execute are human-only. Not configurable and independent of the role switches."""
    _charge(request, principal, "mutating")
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
VerifyPrincipal = Annotated[Principal, Depends(require_verify)]
CollectPrincipal = Annotated[Principal, Depends(require_collect)]
InvestigatePrincipal = Annotated[Principal, Depends(require_investigate)]
DecidingPrincipal = Annotated[Principal, Depends(require_decider)]

# Every route that is not public must depend on one of these (tests/integration/test_api_identity.py).
ROUTE_GUARDS = (require_read, require_verify, require_collect, require_investigate, require_decider)
