"""Builders for ``X-MS-CLIENT-PRINCIPAL`` headers shaped like the ones Container Apps authentication sends."""

from __future__ import annotations

import base64
import json
from collections.abc import Iterable

OID_URI = "http://schemas.microsoft.com/identity/claims/objectidentifier"
SUB_URI = "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/nameidentifier"
SCOPE_URI = "http://schemas.microsoft.com/identity/claims/scope"
ROLE_URI = "http://schemas.microsoft.com/ws/2008/06/identity/claims/role"

Claim = tuple[str, str]


def encode(claims: Iterable[Claim], *, role_typ: str = "roles", name_typ: str = "name") -> str:
    payload = {
        "auth_typ": "aad",
        "name_typ": name_typ,
        "role_typ": role_typ,
        "claims": [{"typ": typ, "val": val} for typ, val in claims],
    }
    return base64.b64encode(json.dumps(payload).encode()).decode()


def header(claims: Iterable[Claim], **kwargs: str) -> dict[str, str]:
    return {"X-MS-CLIENT-PRINCIPAL": encode(claims, **kwargs)}


def _roles(roles: Iterable[str]) -> list[Claim]:
    return [("roles", role) for role in roles]


def user_access_token(oid: str, roles: Iterable[str] = (), *, name: str = "Olivia Operator") -> list[Claim]:
    """Delegated access token for a signed-in user: ``scp`` present, pairwise ``sub``."""
    return [
        (OID_URI, oid),
        (SUB_URI, f"pairwise-{oid}"),
        (SCOPE_URI, "FabricOps.Access"),
        ("name", name),
        ("preferred_username", f"{oid}@contoso.com"),
        ("ver", "2.0"),
        *_roles(roles),
    ]


def user_id_token(oid: str, roles: Iterable[str] = (), *, name: str = "Olivia Operator") -> list[Claim]:
    """ID token of a browser session (Easy Auth cookie): no ``scp``, no ``idtyp``, pairwise ``sub``."""
    return [
        (OID_URI, oid),
        (SUB_URI, f"pairwise-{oid}"),
        ("name", name),
        ("preferred_username", f"{oid}@contoso.com"),
        ("ver", "2.0"),
        *_roles(roles),
    ]


def app_token(oid: str, roles: Iterable[str] = ()) -> list[Claim]:
    """Client-credentials token (service principal or managed identity) with every app-only signal."""
    return [
        (OID_URI, oid),
        (SUB_URI, oid),
        ("idtyp", "app"),
        ("azp", "11111111-1111-1111-1111-111111111111"),
        ("azpacr", "2"),
        ("ver", "2.0"),
        *_roles(roles),
    ]


def app_idtyp_only(oid: str, roles: Iterable[str] = ()) -> list[Claim]:
    """Only ``idtyp=app`` marks it as an app (``sub`` is missing)."""
    return [(OID_URI, oid), ("idtyp", "app"), *_roles(roles)]


def app_sub_equals_oid_only(oid: str, roles: Iterable[str] = ()) -> list[Claim]:
    """Only ``sub == oid`` marks it as an app (optional ``idtyp`` claim not configured)."""
    return [(OID_URI, oid), (SUB_URI, oid), *_roles(roles)]


def roles_without_scp_only(oid: str, roles: Iterable[str] = ()) -> list[Claim]:
    """Only roles and no ``scp``: an app token or an ID token, nothing more to go on."""
    return [(OID_URI, oid), *_roles(roles)]
