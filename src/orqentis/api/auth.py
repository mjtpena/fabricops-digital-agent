"""Caller identity for the API.

* ``easy_auth``: Azure Container Apps / App Service built-in authentication validates the Entra ID
  token and forwards ``X-MS-CLIENT-PRINCIPAL`` (base64 JSON claims). The app must only be reachable
  through that front end.
* ``dev_headers``: local development only (rejected by Settings outside ``local``).
"""

from __future__ import annotations

import base64
import binascii
import json
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status

from orqentis.config.settings import ApiAuthMode, Settings
from orqentis.domain.principals import Principal, PrincipalKind

OID_CLAIM = "http://schemas.microsoft.com/identity/claims/objectidentifier"
_RESERVED_PREFIXES = ("agent:", "system:")


def _from_easy_auth(header: str | None) -> Principal:
    if not header:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing X-MS-CLIENT-PRINCIPAL (Entra ID authentication)")
    try:
        payload = json.loads(base64.b64decode(header))
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Malformed client principal") from exc
    claims = payload.get("claims") or []
    role_type = payload.get("role_typ", "roles")
    name_type = payload.get("name_typ", "name")
    values = {c.get("typ"): c.get("val") for c in claims if isinstance(c, dict)}
    principal_id = values.get(OID_CLAIM) or values.get("oid") or values.get(name_type)
    if not principal_id:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Client principal has no object id")
    roles = frozenset(c.get("val") for c in claims if isinstance(c, dict) and c.get("typ") in {role_type, "roles"})
    return Principal(
        id=str(principal_id),
        display_name=str(values.get(name_type) or principal_id),
        kind=PrincipalKind.HUMAN,
        roles=frozenset(r for r in roles if r),
    )


def _from_dev_headers(user: str | None, roles: str | None) -> Principal:
    principal_id = (user or "dev-user@localhost").strip()
    if principal_id.startswith(_RESERVED_PREFIXES):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Reserved principal id")
    role_set = frozenset(r.strip() for r in (roles or "").split(",") if r.strip())
    return Principal(id=principal_id, display_name=principal_id, kind=PrincipalKind.HUMAN, roles=role_set)


def current_principal(
    request: Request,
    x_ms_client_principal: Annotated[str | None, Header()] = None,
    x_orqentis_user: Annotated[str | None, Header()] = None,
    x_orqentis_roles: Annotated[str | None, Header()] = None,
) -> Principal:
    settings: Settings = request.app.state.settings
    if settings.api_auth_mode is ApiAuthMode.EASY_AUTH:
        return _from_easy_auth(x_ms_client_principal)
    return _from_dev_headers(x_orqentis_user, x_orqentis_roles)


CurrentPrincipal = Annotated[Principal, Depends(current_principal)]
