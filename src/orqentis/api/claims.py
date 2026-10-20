"""Easy Auth client principal: parsing and classification into HUMAN or SERVICE.

Container Apps authentication validates the Entra ID token (signature, issuer, audience, lifetime) and
forwards the claims in ``X-MS-CLIENT-PRINCIPAL``. This module decides *what kind of caller* those claims
describe. Approving and executing mutations are reserved for ``PrincipalKind.HUMAN``, so the rule is fail
closed: a principal is HUMAN only on positive evidence of a user token and never when any signal says the
token is app-only or when the claims contradict each other. The decision table is in ADR-0015.

Signals (Microsoft identity platform claims reference):

* ``idtyp`` (optional claim, access tokens): ``app`` for app-only tokens. It is emitted only when the API's
  app registration asks for it, so a missing ``idtyp`` proves nothing.
* ``scp``: "only included for user tokens". Its absence means an app-only token **or an ID token**, so
  ``roles`` without ``scp`` cannot by itself mean "service": ID-token browser sessions look the same.
* ``sub`` / ``oid``: ``sub`` is a pairwise identifier per user and application, ``oid`` is the object id. App
  tokens carry the service principal's object id in both, so ``sub == oid`` marks an app token. A user token
  must show ``sub != oid``.
* ``FabricOps.Automation`` role: only Application members can hold it.
"""

from __future__ import annotations

import base64
import binascii
import json
from enum import StrEnum

from orqentis.api.roles import ROLE_AUTOMATION
from orqentis.domain.common import DomainModel
from orqentis.domain.principals import PrincipalKind

# Easy Auth forwards some claims under their legacy WS-Federation names. Fold both spellings to the JWT name.
_ALIASES = {
    "http://schemas.microsoft.com/identity/claims/objectidentifier": "oid",
    "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/nameidentifier": "sub",
    "http://schemas.microsoft.com/identity/claims/scope": "scp",
    "http://schemas.microsoft.com/identity/claims/tenantid": "tid",
    "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/upn": "upn",
    "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress": "email",
    "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name": "name",
}
_ROLE_CLAIM_TYPES = frozenset({"roles", "http://schemas.microsoft.com/ws/2008/06/identity/claims/role"})
# Claims only a user token carries. Used as corroboration for ID-token sessions, never as an identity.
_USER_IDENTITY_CLAIMS = ("preferred_username", "upn", "email", "unique_name", "name")
_SUPPORTED_IDTYP = frozenset({"app", "user"})
_MAX_DISPLAY_NAME = 200


class MalformedClientPrincipalError(ValueError):
    """The header is not a base64 JSON client principal."""


class Rule(StrEnum):
    """Which row of the decision table decided (see ADR-0015)."""

    APP_ONLY = "APP_ONLY"
    DELEGATED_ACCESS_TOKEN = "DELEGATED_ACCESS_TOKEN"  # noqa: S105 (a rule id, not a credential)
    USER_TOKEN_NO_SCOPE = "USER_TOKEN_NO_SCOPE"  # noqa: S105 (a rule id, not a credential)
    NO_OBJECT_ID = "NO_OBJECT_ID"
    CONFLICTING_CLAIMS = "CONFLICTING_CLAIMS"
    UNSUPPORTED_IDTYP = "UNSUPPORTED_IDTYP"
    CONTRADICTORY_SIGNALS = "CONTRADICTORY_SIGNALS"
    NO_SUBJECT = "NO_SUBJECT"
    NO_USER_IDENTITY = "NO_USER_IDENTITY"


class Classification(DomainModel):
    kind: PrincipalKind
    rule: Rule
    # True when the claims could not be interpreted with confidence. Such a principal is never HUMAN and the
    # API refuses it outright (403) instead of serving it as a service.
    ambiguous: bool
    detail: str


class ClientPrincipal(DomainModel):
    """Claims of an Easy Auth principal, keyed by their canonical (JWT) name."""

    claims: dict[str, tuple[str, ...]]
    name_claim: str = "name"

    def values(self, name: str) -> tuple[str, ...]:
        """Distinct, non-empty values of a claim, in order of appearance."""
        seen: dict[str, None] = {}
        for value in self.claims.get(name, ()):
            if value:
                seen.setdefault(value)
        return tuple(seen)

    def first(self, name: str) -> str | None:
        found = self.values(name)
        return found[0] if found else None

    @property
    def object_id(self) -> str | None:
        return self.first("oid")

    @property
    def roles(self) -> frozenset[str]:
        return frozenset(self.values("roles"))

    @property
    def display_name(self) -> str:
        for name in (self.name_claim, "name", "preferred_username"):
            if value := self.first(name):
                return value[:_MAX_DISPLAY_NAME]
        return (self.object_id or "")[:_MAX_DISPLAY_NAME]


def parse_client_principal(header: str) -> ClientPrincipal:
    try:
        payload = json.loads(base64.b64decode(header))
    except (binascii.Error, ValueError) as exc:
        raise MalformedClientPrincipalError("not base64 JSON") from exc
    if not isinstance(payload, dict):
        raise MalformedClientPrincipalError("not a JSON object")
    role_typ = payload.get("role_typ")
    role_types = _ROLE_CLAIM_TYPES | ({role_typ} if isinstance(role_typ, str) else set())
    name_typ = payload.get("name_typ")
    claims: dict[str, list[str]] = {}
    raw_claims = payload.get("claims")
    for claim in raw_claims if isinstance(raw_claims, list) else []:
        if not isinstance(claim, dict):
            continue
        typ, value = claim.get("typ"), claim.get("val")
        if not isinstance(typ, str) or not isinstance(value, str):
            continue
        name = "roles" if typ in role_types else _ALIASES.get(typ, typ)
        claims.setdefault(name, []).append(value.strip())
    name_claim = _ALIASES.get(name_typ, name_typ) if isinstance(name_typ, str) else "name"
    return ClientPrincipal(claims={k: tuple(v) for k, v in claims.items()}, name_claim=name_claim)


def object_id_from_header(header: str | None) -> str | None:
    """Best-effort object id for keying (rate limits). Never raises, never authenticates."""
    if not header:
        return None
    try:
        return parse_client_principal(header).object_id
    except MalformedClientPrincipalError:
        return None


def _same(a: str, b: str) -> bool:
    return a.strip().lower() == b.strip().lower()


def _refuse(rule: Rule, detail: str) -> Classification:
    return Classification(kind=PrincipalKind.SERVICE, rule=rule, ambiguous=True, detail=detail)


def classify(client: ClientPrincipal) -> Classification:
    """Decide HUMAN or SERVICE. HUMAN needs positive user evidence and no app or contradictory signal."""
    conflicting = [name for name in ("oid", "sub", "idtyp") if len(client.values(name)) > 1]
    if conflicting:
        return _refuse(Rule.CONFLICTING_CLAIMS, f"multiple different values for {', '.join(conflicting)}")
    oid = client.first("oid")
    if oid is None:
        return _refuse(Rule.NO_OBJECT_ID, "no object id (oid) claim")
    sub = client.first("sub")
    idtyp = (client.first("idtyp") or "").lower() or None
    if idtyp is not None and idtyp not in _SUPPORTED_IDTYP:
        return _refuse(Rule.UNSUPPORTED_IDTYP, f"idtyp={idtyp!r} is neither 'app' nor 'user'")
    has_scp = client.first("scp") is not None

    app_signals = []
    if idtyp == "app":
        app_signals.append("idtyp=app")
    if sub is not None and _same(sub, oid):
        app_signals.append("sub==oid")
    if ROLE_AUTOMATION in client.roles:
        app_signals.append(f"role {ROLE_AUTOMATION}")
    user_signals = []
    if has_scp:
        user_signals.append("scp present")
    if idtyp == "user":
        user_signals.append("idtyp=user")

    if app_signals and user_signals:
        return _refuse(
            Rule.CONTRADICTORY_SIGNALS,
            f"app-only signal ({', '.join(app_signals)}) together with user signal ({', '.join(user_signals)})",
        )
    if app_signals:
        return Classification(
            kind=PrincipalKind.SERVICE, rule=Rule.APP_ONLY, ambiguous=False, detail=", ".join(app_signals)
        )
    if sub is None:
        return _refuse(Rule.NO_SUBJECT, "no sub claim: cannot show the pairwise subject of a user token")
    if has_scp:
        return Classification(
            kind=PrincipalKind.HUMAN, rule=Rule.DELEGATED_ACCESS_TOKEN, ambiguous=False, detail="scp present, sub!=oid"
        )
    if idtyp == "user" or any(client.first(name) for name in _USER_IDENTITY_CLAIMS):
        return Classification(
            kind=PrincipalKind.HUMAN,
            rule=Rule.USER_TOKEN_NO_SCOPE,
            ambiguous=False,
            detail="no scp, sub!=oid, user identity claims present (ID-token session)",
        )
    return _refuse(Rule.NO_USER_IDENTITY, "no scp and no user identity claim: cannot tell a user from an app")
