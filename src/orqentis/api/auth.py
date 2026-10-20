"""Caller identity for the API.

* ``easy_auth``: Azure Container Apps / App Service built-in authentication validates the Entra ID
  token and forwards ``X-MS-CLIENT-PRINCIPAL`` (base64 JSON claims). The app must only be reachable
  through that front end. The claims decide whether the caller is a HUMAN or a SERVICE
  (``orqentis.api.claims``): app-only tokens are never HUMAN, so they can never approve or execute.
* ``dev_headers``: local development only (rejected by Settings outside ``local``).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status

from orqentis.api.claims import (
    Classification,
    MalformedClientPrincipalError,
    Rule,
    classify,
    parse_client_principal,
)
from orqentis.api.errors import ApiError
from orqentis.config.settings import ApiAuthMode, Settings
from orqentis.domain.principals import Principal, PrincipalKind
from orqentis.observability.logging import get_logger

_log = get_logger(__name__)

_RESERVED_PREFIXES = ("agent:", "system:")


def classify_client_principal(header: str | None) -> tuple[Principal, Classification]:
    """Turn an ``X-MS-CLIENT-PRINCIPAL`` header into a principal and the decision that classified it.

    Raises 401 for a missing, malformed or object-id-less principal. An ambiguous or contradictory claim
    set is not served: it is refused with 403 ``PRINCIPAL_AMBIGUOUS`` and is never HUMAN.
    """
    if not header:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing X-MS-CLIENT-PRINCIPAL (Entra ID authentication)")
    try:
        client = parse_client_principal(header)
    except MalformedClientPrincipalError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Malformed client principal") from exc
    decision = classify(client)
    if decision.rule is Rule.NO_OBJECT_ID:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Client principal has no object id")
    principal_id = client.object_id
    assert principal_id is not None  # NO_OBJECT_ID handled above
    if decision.ambiguous:
        _log.warning(
            "ambiguous client principal refused",
            extra={"rule": decision.rule.value, "principal_id": principal_id, "detail": decision.detail},
        )
        raise ApiError(
            status.HTTP_403_FORBIDDEN,
            "PRINCIPAL_AMBIGUOUS",
            f"The caller's token claims cannot be classified as a user or an application ({decision.rule.value})",
        )
    if principal_id.startswith(_RESERVED_PREFIXES):
        raise ApiError(status.HTTP_403_FORBIDDEN, "RESERVED_PRINCIPAL", "Reserved principal id")
    principal = Principal(id=principal_id, display_name=client.display_name, kind=decision.kind, roles=client.roles)
    return principal, decision


def _from_easy_auth(header: str | None) -> Principal:
    return classify_client_principal(header)[0]


def _from_dev_headers(user: str | None, roles: str | None) -> Principal:
    principal_id = (user or "dev-user@localhost").strip()
    if principal_id.startswith(_RESERVED_PREFIXES):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Reserved principal id")
    role_set = frozenset(r.strip() for r in (roles or "").split(",") if r.strip())
    return Principal(id=principal_id, display_name=principal_id, kind=PrincipalKind.HUMAN, roles=role_set)


def _single_header(request: Request, name: str) -> str | None:
    """The value of a header that must be present at most once.

    Starlette would silently return the first of several. A forged value placed in front of the genuine one
    must not win, so more than one copy refuses the request.
    """
    values = request.headers.getlist(name)
    if len(values) > 1:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"More than one {name} header")
    return values[0] if values else None


def current_principal(
    request: Request,
    x_ms_client_principal: Annotated[str | None, Header()] = None,
    x_orqentis_user: Annotated[str | None, Header()] = None,
    x_orqentis_roles: Annotated[str | None, Header()] = None,
) -> Principal:
    # The Header() parameters document the inputs in OpenAPI; the values are read from the raw request so that
    # duplicated headers can be refused.
    settings: Settings = request.app.state.settings
    if settings.api_auth_mode is ApiAuthMode.EASY_AUTH:
        return _from_easy_auth(_single_header(request, "x-ms-client-principal"))
    return _from_dev_headers(_single_header(request, "x-orqentis-user"), _single_header(request, "x-orqentis-roles"))


CurrentPrincipal = Annotated[Principal, Depends(current_principal)]
