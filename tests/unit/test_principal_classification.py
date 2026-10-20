"""Principal classification from Easy Auth token claims (ADR-0015 decision table).

An app-only token must never become a HUMAN principal, because humans are the only callers allowed to
approve and execute. Every row of the decision table is tested, plus an exhaustive sweep over signal
combinations that asserts the safety property directly.
"""

from __future__ import annotations

import base64
import itertools
import json

import pytest
from fastapi import HTTPException

from orqentis.api.auth import classify_client_principal
from orqentis.api.claims import Classification, Rule, classify, parse_client_principal
from orqentis.api.errors import ApiError
from orqentis.api.roles import ROLE_AUTOMATION, ROLE_OPERATOR, ROLE_PLATFORM_ADMIN, ROLE_READER
from orqentis.domain.principals import PrincipalKind
from tests.support import easy_auth as ea
from tests.support.easy_auth import Claim

pytestmark = pytest.mark.security

OID = "0b6f0c1e-1111-4222-8333-444455556666"
ALL_ROLES = [ROLE_READER, ROLE_AUTOMATION, ROLE_OPERATOR, ROLE_PLATFORM_ADMIN]


def classification(claims: list[Claim], **kwargs: str) -> Classification:
    return classify(parse_client_principal(ea.encode(claims, **kwargs)))


# (id, claims, kind, rule, ambiguous)
TABLE: list[tuple[str, list[Claim], PrincipalKind, Rule, bool]] = [
    # --- HUMAN: positive evidence of a user token, no app signal -----------------------------------------
    (
        "delegated-access-token",
        ea.user_access_token(OID, [ROLE_OPERATOR]),
        PrincipalKind.HUMAN,
        Rule.DELEGATED_ACCESS_TOKEN,
        False,
    ),
    (
        "delegated-access-token-idtyp-user",
        [*ea.user_access_token(OID, [ROLE_OPERATOR]), ("idtyp", "user")],
        PrincipalKind.HUMAN,
        Rule.DELEGATED_ACCESS_TOKEN,
        False,
    ),
    ("id-token-session", ea.user_id_token(OID, [ROLE_OPERATOR]), PrincipalKind.HUMAN, Rule.USER_TOKEN_NO_SCOPE, False),
    (
        "id-token-session-no-roles",
        ea.user_id_token(OID),
        PrincipalKind.HUMAN,
        Rule.USER_TOKEN_NO_SCOPE,
        False,
    ),
    (
        "user-access-token-without-scp-idtyp-user",
        [(ea.OID_URI, OID), (ea.SUB_URI, "pairwise"), ("idtyp", "user"), ("roles", ROLE_OPERATOR)],
        PrincipalKind.HUMAN,
        Rule.USER_TOKEN_NO_SCOPE,
        False,
    ),
    (
        "id-token-with-email-only",
        [(ea.OID_URI, OID), (ea.SUB_URI, "pairwise"), ("email", "o@contoso.com")],
        PrincipalKind.HUMAN,
        Rule.USER_TOKEN_NO_SCOPE,
        False,
    ),
    # --- SERVICE: app-only, each signal on its own and combined ------------------------------------------
    ("app-all-signals", ea.app_token(OID, ALL_ROLES), PrincipalKind.SERVICE, Rule.APP_ONLY, False),
    ("app-idtyp-only", ea.app_idtyp_only(OID, [ROLE_PLATFORM_ADMIN]), PrincipalKind.SERVICE, Rule.APP_ONLY, False),
    (
        "app-sub-equals-oid-only",
        ea.app_sub_equals_oid_only(OID, [ROLE_PLATFORM_ADMIN]),
        PrincipalKind.SERVICE,
        Rule.APP_ONLY,
        False,
    ),
    (
        "app-sub-equals-oid-case-insensitive",
        [(ea.OID_URI, OID.upper()), (ea.SUB_URI, OID.lower()), ("roles", ROLE_OPERATOR)],
        PrincipalKind.SERVICE,
        Rule.APP_ONLY,
        False,
    ),
    (
        "app-idtyp-uppercase",
        [(ea.OID_URI, OID), (ea.SUB_URI, "pairwise"), ("idtyp", "APP"), ("name", "n")],
        PrincipalKind.SERVICE,
        Rule.APP_ONLY,
        False,
    ),
    (
        "automation-role-marks-an-application",
        [*ea.user_id_token(OID), ("roles", ROLE_AUTOMATION)],
        PrincipalKind.SERVICE,
        Rule.APP_ONLY,
        False,
    ),
    # --- SERVICE, ambiguous: never HUMAN, refused by the API ---------------------------------------------
    (
        "roles-without-scp-and-nothing-else",
        ea.roles_without_scp_only(OID, [ROLE_PLATFORM_ADMIN]),
        PrincipalKind.SERVICE,
        Rule.NO_SUBJECT,
        True,
    ),
    (
        "scp-without-sub",
        [(ea.OID_URI, OID), (ea.SCOPE_URI, "FabricOps.Access"), ("name", "n")],
        PrincipalKind.SERVICE,
        Rule.NO_SUBJECT,
        True,
    ),
    (
        "no-scp-pairwise-sub-but-no-user-identity",
        [(ea.OID_URI, OID), (ea.SUB_URI, "pairwise"), ("roles", ROLE_OPERATOR)],
        PrincipalKind.SERVICE,
        Rule.NO_USER_IDENTITY,
        True,
    ),
    (
        "empty-scp-is-absent",
        [(ea.OID_URI, OID), (ea.SUB_URI, "pairwise"), (ea.SCOPE_URI, "  "), ("roles", ROLE_OPERATOR)],
        PrincipalKind.SERVICE,
        Rule.NO_USER_IDENTITY,
        True,
    ),
    (
        "idtyp-app-with-scp",
        [*ea.user_access_token(OID, [ROLE_OPERATOR]), ("idtyp", "app")],
        PrincipalKind.SERVICE,
        Rule.CONTRADICTORY_SIGNALS,
        True,
    ),
    (
        "sub-equals-oid-with-scp",
        [(ea.OID_URI, OID), (ea.SUB_URI, OID), (ea.SCOPE_URI, "FabricOps.Access"), ("name", "n")],
        PrincipalKind.SERVICE,
        Rule.CONTRADICTORY_SIGNALS,
        True,
    ),
    (
        "idtyp-user-with-sub-equals-oid",
        [(ea.OID_URI, OID), (ea.SUB_URI, OID), ("idtyp", "user"), ("name", "n")],
        PrincipalKind.SERVICE,
        Rule.CONTRADICTORY_SIGNALS,
        True,
    ),
    (
        "automation-role-with-scp",
        ea.user_access_token(OID, [ROLE_OPERATOR, ROLE_AUTOMATION]),
        PrincipalKind.SERVICE,
        Rule.CONTRADICTORY_SIGNALS,
        True,
    ),
    (
        "idtyp-device",
        [*ea.user_access_token(OID, [ROLE_OPERATOR]), ("idtyp", "device")],
        PrincipalKind.SERVICE,
        Rule.UNSUPPORTED_IDTYP,
        True,
    ),
    (
        "idtyp-unknown-value",
        [*ea.user_id_token(OID, [ROLE_OPERATOR]), ("idtyp", "human")],
        PrincipalKind.SERVICE,
        Rule.UNSUPPORTED_IDTYP,
        True,
    ),
    (
        "conflicting-oid",
        [*ea.user_access_token(OID, [ROLE_OPERATOR]), (ea.OID_URI, "another-oid")],
        PrincipalKind.SERVICE,
        Rule.CONFLICTING_CLAIMS,
        True,
    ),
    (
        "conflicting-sub",
        [*ea.user_access_token(OID, [ROLE_OPERATOR]), ("sub", "other-sub")],
        PrincipalKind.SERVICE,
        Rule.CONFLICTING_CLAIMS,
        True,
    ),
    (
        "conflicting-idtyp",
        [*ea.app_token(OID, [ROLE_OPERATOR]), ("idtyp", "user")],
        PrincipalKind.SERVICE,
        Rule.CONFLICTING_CLAIMS,
        True,
    ),
    (
        "no-object-id",
        [(ea.SUB_URI, "pairwise"), (ea.SCOPE_URI, "x"), ("name", "n")],
        PrincipalKind.SERVICE,
        Rule.NO_OBJECT_ID,
        True,
    ),
    ("no-claims", [], PrincipalKind.SERVICE, Rule.NO_OBJECT_ID, True),
]


@pytest.mark.parametrize(("claims", "kind", "rule", "ambiguous"), [t[1:] for t in TABLE], ids=[t[0] for t in TABLE])
def test_decision_table_row(claims: list[Claim], kind: PrincipalKind, rule: Rule, ambiguous: bool) -> None:
    result = classification(claims)
    assert (result.kind, result.rule, result.ambiguous) == (kind, rule, ambiguous), result.detail


def test_table_covers_every_rule() -> None:
    assert {t[3] for t in TABLE} == set(Rule)


def test_only_unambiguous_user_rows_are_human() -> None:
    for name, claims, kind, _, ambiguous in TABLE:
        result = classification(claims)
        assert (result.kind is PrincipalKind.HUMAN) == (kind is PrincipalKind.HUMAN), name
        if result.kind is PrincipalKind.HUMAN:
            assert not result.ambiguous, name
        assert ambiguous == result.ambiguous, name


def test_claim_names_are_recognised_in_jwt_and_ws_federation_spelling() -> None:
    long_names = [
        (ea.OID_URI, OID),
        (ea.SUB_URI, "pairwise"),
        (ea.SCOPE_URI, "FabricOps.Access"),
        (ea.ROLE_URI, ROLE_OPERATOR),
    ]
    short_names = [("oid", OID), ("sub", "pairwise"), ("scp", "FabricOps.Access"), ("roles", ROLE_OPERATOR)]
    for claims, kwargs in ((long_names, {"role_typ": ea.ROLE_URI}), (short_names, {})):
        result = classification(claims, **kwargs)
        assert result.kind is PrincipalKind.HUMAN
        principal, _ = classify_client_principal(ea.encode(claims, **kwargs))
        assert principal.id == OID and principal.roles == {ROLE_OPERATOR}
    app_long = [(ea.OID_URI, OID), (ea.SUB_URI, OID)]
    assert classification(app_long).kind is PrincipalKind.SERVICE


@pytest.mark.parametrize(
    "builder",
    [ea.app_token, ea.app_idtyp_only, ea.app_sub_equals_oid_only, ea.roles_without_scp_only],
    ids=["all-signals", "idtyp-only", "sub-equals-oid-only", "roles-without-scp-only"],
)
@pytest.mark.parametrize("n_roles", range(len(ALL_ROLES) + 1))
def test_app_only_tokens_with_any_roles_are_never_human(builder: object, n_roles: int) -> None:
    for roles in itertools.combinations(ALL_ROLES, n_roles):
        claims = builder(OID, roles)  # type: ignore[operator]
        assert classification(claims).kind is not PrincipalKind.HUMAN, roles
        if classification(claims).ambiguous:
            with pytest.raises(ApiError) as refused:
                classify_client_principal(ea.encode(claims))
            assert refused.value.status_code == 403 and refused.value.code == "PRINCIPAL_AMBIGUOUS"
        else:
            principal, _ = classify_client_principal(ea.encode(claims))
            assert principal.kind is PrincipalKind.SERVICE
            assert principal.roles == set(roles)  # roles are kept: SERVICE is blocked by kind, not by stripping


def test_exhaustive_signal_sweep_never_yields_human_from_an_app_signal() -> None:
    """Whatever the combination of claims, HUMAN implies positive user evidence and no app evidence."""
    oid_options: list[list[str]] = [[], [OID], [OID, "other"]]
    sub_options: list[list[str]] = [[], [OID], [OID.upper()], ["pairwise"], ["pairwise", "pairwise-2"]]
    scp_options: list[list[str]] = [[], [""], ["FabricOps.Access"]]
    idtyp_options: list[list[str]] = [[], ["app"], ["APP"], ["user"], ["device"], ["app", "user"]]
    role_options: list[list[str]] = [[], [ROLE_OPERATOR], [ROLE_AUTOMATION], [ROLE_OPERATOR, ROLE_AUTOMATION]]
    identity_options: list[list[Claim]] = [[], [("name", "n")], [("preferred_username", "p")]]
    humans = 0
    for oids, subs, scps, idtyps, roles, identity in itertools.product(
        oid_options, sub_options, scp_options, idtyp_options, role_options, identity_options
    ):
        claims: list[Claim] = [
            *[("oid", v) for v in oids],
            *[("sub", v) for v in subs],
            *[("scp", v) for v in scps],
            *[("idtyp", v) for v in idtyps],
            *[("roles", v) for v in roles],
            *identity,
        ]
        result = classification(claims)
        if result.kind is not PrincipalKind.HUMAN:
            continue
        humans += 1
        label = (oids, subs, scps, idtyps, roles, identity)
        assert not result.ambiguous, label
        assert len(oids) == 1 and len(subs) == 1 and len(idtyps) <= 1, label
        assert subs[0].lower() != oids[0].lower(), label  # pairwise subject, not the object id
        assert not idtyps or idtyps[0] == "user", label
        assert ROLE_AUTOMATION not in roles, label
        assert any(scps) or idtyps == ["user"] or identity, label  # scp, user idtyp or a user identity claim
    assert humans > 0


def test_missing_and_malformed_headers_are_401() -> None:
    for bad in (None, "", "%%%", base64.b64encode(b"not json").decode(), base64.b64encode(b"[1, 2]").decode()):
        with pytest.raises(HTTPException) as refused:
            classify_client_principal(bad)
        assert refused.value.status_code == 401, bad


def test_principal_without_claims_or_with_odd_claim_shapes_is_401_not_500() -> None:
    odd = {
        "claims": [None, "x", {"typ": 1, "val": "x"}, {"typ": "oid", "val": ["a"]}, {"typ": "roles"}],
        "role_typ": 5,
        "name_typ": ["name"],
    }
    header = base64.b64encode(json.dumps(odd).encode()).decode()
    with pytest.raises(HTTPException) as refused:
        classify_client_principal(header)
    assert refused.value.status_code == 401
    no_claims = base64.b64encode(json.dumps({"claims": "nope"}).encode()).decode()
    with pytest.raises(HTTPException) as refused:
        classify_client_principal(no_claims)
    assert refused.value.status_code == 401


def test_ambiguous_principal_is_refused_with_403_and_a_code() -> None:
    with pytest.raises(ApiError) as refused:
        classify_client_principal(ea.encode(ea.roles_without_scp_only(OID, [ROLE_PLATFORM_ADMIN])))
    assert refused.value.status_code == 403 and refused.value.code == "PRINCIPAL_AMBIGUOUS"


def test_reserved_object_ids_are_refused() -> None:
    for reserved in ("agent:FabricOpsInvestigator", "system:orqentis"):
        claims = [(ea.OID_URI, reserved), (ea.SUB_URI, "pairwise"), (ea.SCOPE_URI, "x"), ("name", "n")]
        with pytest.raises(ApiError) as refused:
            classify_client_principal(ea.encode(claims))
        assert refused.value.status_code == 403 and refused.value.code == "RESERVED_PRINCIPAL"


def test_display_name_comes_from_an_allowed_name_typ_and_is_bounded() -> None:
    claims = [c for c in ea.user_access_token(OID) if c[0] not in {"name", "preferred_username"}]
    claims.append(("preferred_username", "D" * 500))
    principal, _ = classify_client_principal(ea.encode(claims, name_typ="preferred_username"))
    assert principal.display_name == "D" * 200
    principal, _ = classify_client_principal(ea.encode(ea.user_access_token(OID, name="Olivia Operator")))
    assert principal.display_name == "Olivia Operator"
    # No name claim at all: fall back to the object id.
    bare = [(ea.OID_URI, OID), (ea.SUB_URI, "pairwise"), (ea.SCOPE_URI, "x")]
    assert classify_client_principal(ea.encode(bare))[0].display_name == OID


def test_a_name_typ_outside_the_allow_list_is_ignored() -> None:
    claims = [*ea.user_access_token(OID), ("http://example/display", "Mallory")]
    principal, _ = classify_client_principal(ea.encode(claims, name_typ="http://example/display"))
    assert principal.display_name == "Olivia Operator"


def test_name_and_role_claim_types_follow_the_principal_payload() -> None:
    claims = [
        (ea.OID_URI, OID),
        (ea.SUB_URI, "pairwise"),
        (ea.SCOPE_URI, "x"),
        (ea.ROLE_URI, ROLE_OPERATOR),
        ("roles", ROLE_PLATFORM_ADMIN),
    ]
    principal, _ = classify_client_principal(ea.encode(claims, role_typ=ea.ROLE_URI))
    assert principal.roles == {ROLE_OPERATOR, ROLE_PLATFORM_ADMIN}


@pytest.mark.parametrize("role_typ", ["idtyp", "sub", "scp", "oid", "http://example/roles", "azp"])
def test_the_payload_cannot_relabel_a_claim_as_roles(role_typ: str) -> None:
    """Regression: role_typ = "idtyp" folded idtyp=app into the roles and the principal became HUMAN."""
    claims = [*ea.user_id_token(OID, [ROLE_PLATFORM_ADMIN]), ("idtyp", "app"), ("azp", "client")]
    header = ea.encode(claims, role_typ=role_typ)
    client = parse_client_principal(header)
    assert classify(client).kind is PrincipalKind.SERVICE
    assert "app" not in client.roles and role_typ not in client.roles
    assert classify(parse_client_principal(ea.encode(claims))).kind is PrincipalKind.SERVICE


def test_only_the_known_role_claim_types_are_roles() -> None:
    claims = [
        (ea.OID_URI, OID),
        (ea.SUB_URI, "pairwise"),
        (ea.SCOPE_URI, "x"),
        ("http://example/roles", "FabricOps.PlatformAdmin"),
    ]
    client = parse_client_principal(ea.encode(claims, role_typ="http://example/roles"))
    assert client.roles == frozenset()


def test_oversized_or_deeply_nested_principals_are_401_not_500() -> None:
    nested = "[" * 5000 + "]" * 5000
    deep = base64.b64encode(nested.encode()).decode()
    huge = base64.b64encode(json.dumps({"claims": [{"typ": "x", "val": "y" * 40_000}]}).encode()).decode()
    for header in (deep, huge, "A" * 100_000):
        with pytest.raises(HTTPException) as refused:
            classify_client_principal(header)
        assert refused.value.status_code == 401


def test_a_comma_joined_pair_of_headers_is_not_decoded_as_one() -> None:
    genuine = ea.encode(ea.app_token(OID, [ROLE_OPERATOR]))
    forged = ea.encode(ea.user_access_token("oid-forged", [ROLE_PLATFORM_ADMIN]))
    for joined in (f"{forged}, {genuine}", f"{forged},{genuine}", forged + "\n" + genuine, f"{forged} {genuine}"):
        with pytest.raises(HTTPException) as refused:
            classify_client_principal(joined)
        assert refused.value.status_code == 401
