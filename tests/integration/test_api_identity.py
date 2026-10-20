"""Identity and role enforcement over HTTP behind Easy Auth (ADR-0015).

* App-only principals (service principals, managed identities) are SERVICE, never HUMAN: they cannot
  approve, reject or execute, whatever roles their token carries, and nothing reaches the write client.
* People keep working with delegated access tokens and with ID-token browser sessions.
* ``FabricOps.Automation`` may read and trigger collection. It may not investigate, approve, reject or execute.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.routing import Route

from orqentis.api import authz
from orqentis.api import routers as api_routers
from orqentis.api.app import create_app
from orqentis.api.auth import CurrentPrincipal, current_principal
from orqentis.api.roles import ROLE_AUTOMATION, ROLE_OPERATOR, ROLE_PLATFORM_ADMIN, ROLE_READER
from orqentis.bootstrap import Container, build_container
from orqentis.config.settings import ApiAuthMode, Settings
from orqentis.domain.actions import Action
from orqentis.domain.approvals import ApprovalRequest
from tests.conftest import make_settings
from tests.support import easy_auth as ea
from tests.support.jobs import investigate_to_completion

pytestmark = pytest.mark.security

OPERATOR = ea.header(ea.user_access_token("oid-operator", [ROLE_OPERATOR]))
ADMIN_SESSION = ea.header(ea.user_id_token("oid-admin", [ROLE_OPERATOR, ROLE_PLATFORM_ADMIN]))
AUTOMATION = ea.header(ea.app_token("oid-scheduler", [ROLE_AUTOMATION]))
READER = ea.header(ea.user_access_token("oid-reader", [ROLE_READER]))
NO_ROLE = ea.header(ea.user_access_token("oid-norole"))
# Holds an approver role, but not the Operator role the schema-drift approvals require.
ADMIN_ONLY = ea.header(ea.user_access_token("oid-admin-only", [ROLE_PLATFORM_ADMIN]))
ALL_ROLES = [ROLE_READER, ROLE_AUTOMATION, ROLE_OPERATOR, ROLE_PLATFORM_ADMIN]

Headers = dict[str, str]


class SpyWriteClient:
    def __init__(self) -> None:
        self.calls: list[tuple[Action, ApprovalRequest]] = []

    async def execute(self, action: Action, *, approval: ApprovalRequest) -> dict[str, Any]:
        self.calls.append((action, approval))
        return {"executed": True}


@pytest.fixture
def spy() -> SpyWriteClient:
    return SpyWriteClient()


def build_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spy: SpyWriteClient, **overrides: object) -> Any:
    settings = make_settings(tmp_path, api_auth_mode=ApiAuthMode.EASY_AUTH, enable_write_actions=True, **overrides)

    async def build_with_spy(s: Settings) -> Container:
        return await build_container(s, write_client=spy)

    monkeypatch.setattr("orqentis.api.app.build_container", build_with_spy)
    return TestClient(create_app(settings))


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spy: SpyWriteClient) -> Iterator[TestClient]:
    """Easy Auth, write capability ENABLED with a spy: only identity checks can stop a mutation."""
    with build_client(tmp_path, monkeypatch, spy) as test_client:
        yield test_client


def investigate(client: TestClient) -> list[str]:
    incident_id = client.get("/incidents", headers=OPERATOR).json()[0]["incident_id"]
    # POST returns 202 and a job; the investigation runs in the background and the outcome is read from the job.
    job = investigate_to_completion(client, incident_id, OPERATOR)
    approvals: list[str] = job["result"]["approval_ids"]
    assert len(approvals) >= 2
    return approvals


def approval_status(client: TestClient, approval_id: str) -> str:
    status: str = client.get(f"/approvals/{approval_id}", headers=OPERATOR).json()["status"]
    return status


APP_ONLY_PRINCIPALS: list[tuple[str, Callable[[tuple[str, ...]], Headers]]] = [
    ("all-signals", lambda roles: ea.header(ea.app_token("oid-app", roles))),
    ("idtyp-only", lambda roles: ea.header(ea.app_idtyp_only("oid-app", roles))),
    ("sub-equals-oid-only", lambda roles: ea.header(ea.app_sub_equals_oid_only("oid-app", roles))),
    ("roles-without-scp-only", lambda roles: ea.header(ea.roles_without_scp_only("oid-app", roles))),
    (
        "automation-role-on-a-user-looking-token",
        lambda roles: ea.header(ea.user_id_token("oid-app", (*roles, ROLE_AUTOMATION))),
    ),
    (
        "idtyp-app-contradicting-scp",
        lambda roles: ea.header([*ea.user_access_token("oid-app", roles), ("idtyp", "app")]),
    ),
]
ROLE_SETS: list[tuple[str, ...]] = [(), (ROLE_OPERATOR,), (ROLE_PLATFORM_ADMIN,), tuple(ALL_ROLES)]


@pytest.mark.parametrize("roles", ROLE_SETS, ids=["no-roles", "operator", "platform-admin", "every-role"])
@pytest.mark.parametrize(("name", "make"), APP_ONLY_PRINCIPALS, ids=[p[0] for p in APP_ONLY_PRINCIPALS])
def test_app_only_principal_cannot_approve_reject_or_execute(
    client: TestClient,
    spy: SpyWriteClient,
    name: str,
    make: Callable[[tuple[str, ...]], Headers],
    roles: tuple[str, ...],
) -> None:
    approvals = investigate(client)
    attacker = make(roles)
    for verb in ("approve", "reject", "execute"):
        response = client.post(f"/approvals/{approvals[0]}/{verb}", headers=attacker)
        assert response.status_code == 403, (verb, response.text)
        assert response.json()["error"] in {
            "PRINCIPAL_NOT_HUMAN",
            "PRINCIPAL_AMBIGUOUS",
            "AUTOMATION_ROLE_CANNOT_DECIDE",
        }
    assert approval_status(client, approvals[0]) == "PENDING"
    assert spy.calls == []


def test_app_only_principal_cannot_execute_an_approved_action(client: TestClient, spy: SpyWriteClient) -> None:
    approvals = investigate(client)
    assert client.post(f"/approvals/{approvals[0]}/approve", headers=OPERATOR).status_code == 200
    privileged_app = ea.header(ea.app_token("oid-app", ALL_ROLES))
    response = client.post(f"/approvals/{approvals[0]}/execute", headers=privileged_app)
    assert response.status_code == 403 and response.json()["error"] == "PRINCIPAL_NOT_HUMAN"
    assert spy.calls == []
    # The human path still works: the write capability is enabled, so only the identity check stopped the app.
    executed = client.post(f"/approvals/{approvals[0]}/execute", headers=ADMIN_SESSION)
    assert executed.status_code == 200 and executed.json()["status"] == "EXECUTED"
    assert len(spy.calls) == 1


def test_refused_attempts_are_audited_against_the_calling_principal(client: TestClient) -> None:
    approvals = investigate(client)
    client.post(f"/approvals/{approvals[0]}/approve", headers=ea.header(ea.app_token("oid-app", ALL_ROLES)))
    records = client.get("/audit", headers=OPERATOR, params={"limit": 5000}).json()
    refused = [r for r in records if r["action"] == "api.authorize" and r["outcome"] == "REFUSED"]
    assert refused and refused[-1]["actor"] == "oid-app"
    assert refused[-1]["details"]["code"] == "PRINCIPAL_NOT_HUMAN"
    assert client.get("/audit/verify", headers=OPERATOR).json()["valid"] is True


def test_service_principal_is_otherwise_a_working_caller(client: TestClient) -> None:
    """The app-only principal is accepted as SERVICE, so the 403s above come from its kind, not from a 401."""
    app = ea.header(ea.app_token("oid-app", [ROLE_AUTOMATION]))
    assert client.get("/incidents", headers=app).status_code == 200


@pytest.mark.parametrize(
    "claims",
    [ea.user_access_token("oid-u1", [ROLE_OPERATOR]), ea.user_id_token("oid-u2", [ROLE_OPERATOR])],
    ids=["delegated-access-token", "id-token-session"],
)
def test_users_are_human_and_can_approve_with_their_roles(client: TestClient, claims: list[ea.Claim]) -> None:
    approvals = investigate(client)
    approved = client.post(f"/approvals/{approvals[0]}/approve", headers=ea.header(claims), json={"comment": "ok"})
    assert approved.status_code == 200, approved.text
    assert approved.json()["approved_by"] == claims[0][1]


def test_human_with_the_wrong_approver_role_is_still_refused_by_the_service(client: TestClient) -> None:
    approvals = investigate(client)
    response = client.post(f"/approvals/{approvals[0]}/approve", headers=ADMIN_ONLY)
    assert response.status_code == 403 and response.json()["error"] == "APPROVAL_FORBIDDEN"
    # A human with no approver role at all is stopped earlier, before the approval is looked up.
    refused = client.post(f"/approvals/{approvals[0]}/approve", headers=READER)
    assert refused.status_code == 403 and refused.json()["error"] == "ROLE_REQUIRED"


def test_ambiguous_claims_are_refused_everywhere(client: TestClient) -> None:
    ambiguous = ea.header(ea.roles_without_scp_only("oid-x", [ROLE_PLATFORM_ADMIN, ROLE_OPERATOR]))
    for method, path in (("get", "/incidents"), ("post", "/telemetry/collect"), ("get", "/audit")):
        response = getattr(client, method)(path, headers=ambiguous)
        assert response.status_code == 403 and response.json()["error"] == "PRINCIPAL_AMBIGUOUS", path


def test_automation_can_read_and_trigger_collection_but_nothing_else(client: TestClient, spy: SpyWriteClient) -> None:
    for path in ("/incidents", "/runbooks", "/estate/workspaces", "/approvals", "/audit", "/audit/verify"):
        assert client.get(path, headers=AUTOMATION).status_code == 200, path
    collected = client.post("/telemetry/collect", headers=AUTOMATION)
    assert collected.status_code == 200 and "new_events" in collected.json()

    approvals = investigate(client)
    incident_id = client.get("/incidents", headers=AUTOMATION).json()[0]["incident_id"]
    refused = client.post(f"/incidents/{incident_id}/investigate", headers=AUTOMATION)
    assert refused.status_code == 403 and refused.json()["error"] == "ROLE_REQUIRED"
    for verb in ("approve", "reject", "execute"):
        response = client.post(f"/approvals/{approvals[0]}/{verb}", headers=AUTOMATION)
        assert response.status_code == 403 and response.json()["error"] == "PRINCIPAL_NOT_HUMAN", verb
    assert approval_status(client, approvals[0]) == "PENDING"
    assert spy.calls == []


def test_reader_reads_but_cannot_trigger_anything(client: TestClient) -> None:
    for path in ("/incidents", "/runbooks", "/estate/workspaces", "/approvals", "/audit"):
        assert client.get(path, headers=READER).status_code == 200, path
    refused = client.post("/telemetry/collect", headers=READER)
    assert refused.status_code == 403 and refused.json()["error"] == "ROLE_REQUIRED"
    incident_id = client.get("/incidents", headers=READER).json()[0]["incident_id"]
    assert client.post(f"/incidents/{incident_id}/investigate", headers=READER).status_code == 403


def test_operator_and_admin_keep_full_access(client: TestClient) -> None:
    for headers in (OPERATOR, ADMIN_SESSION):
        assert client.get("/incidents", headers=headers).status_code == 200
        assert client.post("/telemetry/collect", headers=headers).status_code == 200


def test_authenticated_caller_without_any_fabricops_role_is_refused_by_default(client: TestClient) -> None:
    for method, path in (("get", "/incidents"), ("get", "/audit"), ("post", "/telemetry/collect")):
        response = getattr(client, method)(path, headers=NO_ROLE)
        assert response.status_code == 403 and response.json()["error"] == "ROLE_REQUIRED", path
    assert client.get("/health").status_code == 200  # health needs no identity


def test_dev_headers_mean_nothing_under_easy_auth(client: TestClient) -> None:
    forged = {"X-Orqentis-User": "adam.admin@contoso.com", "X-Orqentis-Roles": f"{ROLE_OPERATOR},{ROLE_PLATFORM_ADMIN}"}
    assert client.get("/incidents", headers=forged).status_code == 401
    approvals = investigate(client)
    assert client.post(f"/approvals/{approvals[0]}/approve", headers=forged).status_code == 401
    # They also cannot upgrade a real principal: the roles still come from the token alone.
    admin_only_with_forged_roles = {**ADMIN_ONLY, **forged}
    refused = client.post(f"/approvals/{approvals[0]}/approve", headers=admin_only_with_forged_roles)
    assert refused.status_code == 403 and refused.json()["error"] == "APPROVAL_FORBIDDEN"


def test_interactive_docs_and_openapi_are_not_served_under_easy_auth(client: TestClient, tmp_path: Path) -> None:
    """Regression: they used to answer any authenticated caller, including one with no FabricOps role."""
    for headers in (NO_ROLE, OPERATOR, AUTOMATION, {}):
        for path in ("/openapi.json", "/docs", "/redoc", "/docs/oauth2-redirect"):
            assert client.get(path, headers=headers).status_code == 404, path
    # Locally they stay available for development.
    with TestClient(create_app(make_settings(tmp_path))) as local:
        assert local.get("/openapi.json").status_code == 200
        assert local.get("/docs").status_code == 200


def test_missing_principal_is_401_and_health_stays_open(client: TestClient) -> None:
    assert client.get("/incidents").status_code == 401
    assert client.post("/telemetry/collect").status_code == 401
    assert client.get("/health").status_code == 200


def test_turning_off_the_read_role_opens_reads_only_never_collection_or_investigation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spy: SpyWriteClient
) -> None:
    """Regression: api_require_read_role=false used to switch off the roles of collect and investigate as well."""
    role_less_app = ea.header(ea.app_token("oid-app"))
    with build_client(tmp_path, monkeypatch, spy, api_require_read_role=False) as client:
        assert client.get("/incidents", headers=NO_ROLE).status_code == 200
        assert client.get("/incidents", headers=role_less_app).status_code == 200
        incident_id = client.get("/incidents", headers=OPERATOR).json()[0]["incident_id"]
        for headers in (NO_ROLE, role_less_app, READER):
            collect = client.post("/telemetry/collect", headers=headers)
            investigate_ = client.post(f"/incidents/{incident_id}/investigate", headers=headers)
            assert collect.status_code == 403 and collect.json()["error"] == "ROLE_REQUIRED"
            assert investigate_.status_code == 403 and investigate_.json()["error"] == "ROLE_REQUIRED"
        assert client.post("/telemetry/collect", headers=AUTOMATION).status_code == 200
        approvals = client.get("/approvals", headers=OPERATOR).json()
        assert approvals == []  # the role-less callers opened nothing


def test_read_role_enforcement_can_be_turned_on_under_dev_headers(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, api_require_read_role=True)
    with TestClient(create_app(settings)) as client:
        assert client.get("/incidents", headers={"X-Orqentis-User": "v@contoso.com"}).status_code == 403
        reader = {"X-Orqentis-User": "v@contoso.com", "X-Orqentis-Roles": ROLE_READER}
        assert client.get("/incidents", headers=reader).status_code == 200


def test_read_role_switch_is_read_from_the_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORQENTIS_API_REQUIRE_READ_ROLE", "true")
    assert Settings(_env_file=None, data_dir=tmp_path).read_role_required is True  # type: ignore[call-arg]
    monkeypatch.setenv("ORQENTIS_API_REQUIRE_READ_ROLE", "false")
    easy = Settings(
        _env_file=None, data_dir=tmp_path, api_auth_mode=ApiAuthMode.EASY_AUTH, deployment_environment="prod"
    )  # type: ignore[call-arg]
    assert easy.read_role_required is False


def test_action_roles_are_always_on_under_easy_auth(tmp_path: Path) -> None:
    easy = {"api_auth_mode": ApiAuthMode.EASY_AUTH}
    assert make_settings(tmp_path, **easy).action_roles_required is True
    assert make_settings(tmp_path, **easy, api_require_read_role=False).action_roles_required is True
    assert make_settings(tmp_path, **easy, api_require_read_role=True).action_roles_required is True
    # Locally the production role model is opt-in, so demos and existing examples keep working.
    assert make_settings(tmp_path).action_roles_required is False
    assert make_settings(tmp_path, api_require_read_role=True).action_roles_required is True


def test_read_role_default_follows_the_auth_mode(tmp_path: Path) -> None:
    assert make_settings(tmp_path).read_role_required is False
    assert make_settings(tmp_path, api_auth_mode=ApiAuthMode.EASY_AUTH).read_role_required is True
    assert (
        make_settings(tmp_path, api_auth_mode=ApiAuthMode.EASY_AUTH, api_require_read_role=False).read_role_required
        is False
    )


# ---- review round 2: headers, role-first deciding routes, concurrency over HTTP, system info ------------------


def test_two_principal_headers_are_refused_whichever_comes_first(client: TestClient, spy: SpyWriteClient) -> None:
    """Regression: the first header won, so a forged admin principal in front of the genuine one was approved."""
    approvals = investigate(client)
    forged = ea.encode(ea.user_access_token("oid-forged", [ROLE_OPERATOR, ROLE_PLATFORM_ADMIN]))
    genuine = ea.encode(ea.app_token("oid-app", [ROLE_AUTOMATION]))
    for pair in ((forged, genuine), (genuine, forged), (forged, forged)):
        headers = [("X-MS-CLIENT-PRINCIPAL", value) for value in pair]
        for method, path in (("post", f"/approvals/{approvals[0]}/approve"), ("get", "/incidents")):
            response = getattr(client, method)(path, headers=headers)
            assert response.status_code == 401, (path, response.text)
    assert approval_status(client, approvals[0]) == "PENDING" and spy.calls == []


def test_duplicate_dev_headers_are_refused_too(tmp_path: Path) -> None:
    with TestClient(create_app(make_settings(tmp_path))) as local:
        assert (
            local.get(
                "/incidents", headers=[("X-Orqentis-User", "a@c.com"), ("X-Orqentis-User", "b@c.com")]
            ).status_code
            == 401
        )
        assert (
            local.get("/incidents", headers=[("X-Orqentis-Roles", "x"), ("X-Orqentis-Roles", "y")]).status_code == 401
        )
        assert local.get("/incidents", headers={"X-Orqentis-User": "a@c.com"}).status_code == 200


def test_a_caller_without_an_approver_role_cannot_probe_approvals(client: TestClient) -> None:
    """Regression: 404 for an unknown id vs 403/409 for a real one told a role-less caller what exists."""
    approvals = investigate(client)
    for caller in (NO_ROLE, READER, AUTOMATION):
        seen = set()
        for approval_id in (approvals[0], "APR-does-not-exist"):
            for verb in ("approve", "reject", "execute"):
                response = client.post(f"/approvals/{approval_id}/{verb}", headers=caller)
                assert response.status_code == 403, (caller, verb, response.text)
                seen.add((response.json()["error"], response.json()["message"]))
        assert len(seen) <= 3  # the answer does not depend on whether the approval exists


def test_refusals_are_audited_but_cannot_flood_the_audit_log(client: TestClient) -> None:
    approvals = investigate(client)
    before = len(
        [
            r
            for r in client.get("/audit", headers=OPERATOR, params={"limit": 5000}).json()
            if r["action"] == "api.authorize"
        ]
    )
    for index in range(60):
        stranger = ea.header(ea.user_access_token(f"oid-stranger-{index}", []))
        assert client.post(f"/approvals/{approvals[0]}/approve", headers=stranger).status_code == 403
    after = [
        r
        for r in client.get("/audit", headers=OPERATOR, params={"limit": 5000}).json()
        if r["action"] == "api.authorize"
    ]
    assert 0 < len(after) - before <= 20
    assert client.get("/audit/verify", headers=OPERATOR).json()["valid"] is True


def test_concurrent_executes_over_http_reach_the_write_client_once(client: TestClient, spy: SpyWriteClient) -> None:
    approvals = investigate(client)
    assert client.post(f"/approvals/{approvals[0]}/approve", headers=OPERATOR).status_code == 200
    with ThreadPoolExecutor(max_workers=5) as pool:
        codes = list(
            pool.map(
                lambda _: client.post(f"/approvals/{approvals[0]}/execute", headers=ADMIN_SESSION).status_code, range(5)
            )
        )
    assert sorted(codes) == [200, 403, 403, 403, 403]
    assert len(spy.calls) == 1


def test_concurrent_approve_and_reject_over_http_one_wins(client: TestClient) -> None:
    approvals = investigate(client)
    with ThreadPoolExecutor(max_workers=2) as pool:
        approve = pool.submit(client.post, f"/approvals/{approvals[0]}/approve", headers=OPERATOR)
        reject = pool.submit(client.post, f"/approvals/{approvals[0]}/reject", headers=ADMIN_SESSION)
        codes = sorted([approve.result().status_code, reject.result().status_code])
    assert codes == [200, 409]
    final = approval_status(client, approvals[0])
    assert final == ("APPROVED" if approve.result().status_code == 200 else "REJECTED")


def test_health_is_anonymous_and_does_not_disclose_the_write_flag_but_system_info_does(client: TestClient) -> None:
    health = client.get("/health").json()
    assert health["status"] == "ok" and "write_actions_enabled" not in health
    assert client.get("/system/info").status_code == 401
    assert client.get("/system/info", headers=NO_ROLE).status_code == 403
    info = client.get("/system/info", headers=READER).json()
    assert info["write_actions_enabled"] is True and info["fabric_mode"] == "mock"
    assert client.get("/system/info", headers=AUTOMATION).status_code == 200


# ---- structural guard: a new route cannot silently skip authentication, a role check or the human-only check ---

PUBLIC_PATHS = {"/health", "/health/ready"}
DOC_PATHS = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
MUTATIONS = {"POST", "PUT", "PATCH", "DELETE"}
# A kind-only guard (require_human) is deliberately not a role guard.
MUTATION_GUARDS = {authz.require_collect, authz.require_investigate, authz.require_decider}
DECIDING_ROUTES = {
    "/approvals/{approval_id}/approve",
    "/approvals/{approval_id}/reject",
    "/approvals/{approval_id}/execute",
}


def dependency_calls(route: APIRoute) -> set[Any]:
    found: set[Any] = set()
    stack = [route.dependant]
    while stack:
        dependant = stack.pop()
        for sub in dependant.dependencies:
            found.add(sub.call)
            stack.append(sub)
    return found


def router_module_routes() -> list[APIRoute]:
    """Every route of every router module, found without FastAPI internals."""
    routes: list[APIRoute] = []
    for module_info in pkgutil.iter_modules(api_routers.__path__):
        module = importlib.import_module(f"{api_routers.__name__}.{module_info.name}")
        router = getattr(module, "router", None)
        if router is not None:
            routes.extend(r for r in router.routes if isinstance(r, APIRoute))
    return routes


def route_violations(app: FastAPI) -> list[str]:
    """Everything wrong with the routes of a live app: unauthenticated, no role guard, loose mutations."""
    violations: list[str] = []
    reviewed = router_module_routes()
    direct = [r for r in app.routes if isinstance(r, APIRoute)]  # an @app.post() placed on the app itself
    for route in [*reviewed, *direct]:
        calls = dependency_calls(route)
        label = f"{sorted(route.methods)} {route.path}"
        if route.path in PUBLIC_PATHS:
            # Anonymous by design, so nothing under it may read the caller's identity or depend on a role.
            if current_principal in calls or calls & set(authz.ROLE_GUARDS):
                violations.append(f"{label} is public but reads the caller's identity")
            continue
        if current_principal not in calls:
            violations.append(f"{label} does not authenticate")
        if not calls & set(authz.ROLE_GUARDS):
            violations.append(f"{label} has no role guard")
        if route.methods & MUTATIONS and not calls & MUTATION_GUARDS:
            violations.append(f"{label} is a mutation without a collect, investigate or decider guard")
        if route.path in DECIDING_ROUTES and not {authz.require_human, authz.require_decider} <= calls:
            violations.append(f"{label} lost the human-only or approver-role guard")
    # Everything the live app documents must have been inspected above (no operation outside the reviewed set).
    inspected = {(r.path, m) for r in [*reviewed, *direct] for m in r.methods}
    documented = {(path, m.upper()) for path, item in app.openapi()["paths"].items() for m in item}
    violations += [f"{m} {path} is served but was not inspected" for path, m in sorted(documented - inspected)]
    return violations


def test_every_route_is_authenticated_and_role_guarded(tmp_path: Path) -> None:
    for mode in (ApiAuthMode.DEV_HEADERS, ApiAuthMode.EASY_AUTH):
        app = create_app(make_settings(tmp_path, api_auth_mode=mode))
        assert route_violations(app) == []
    assert len(router_module_routes()) >= 15
    assert {r.path for r in router_module_routes() if r.path in DECIDING_ROUTES} == DECIDING_ROUTES


def test_the_route_walk_catches_a_route_added_straight_to_the_app(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path))

    @app.post("/rogue")
    async def rogue() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/rogue-authenticated-only")
    async def rogue_read(_: CurrentPrincipal) -> dict[str, bool]:
        return {"ok": True}

    found = "\n".join(route_violations(app))
    assert "/rogue does not authenticate" in found and "/rogue has no role guard" in found
    assert "/rogue-authenticated-only has no role guard" in found  # a bare CurrentPrincipal is not enough


def test_only_the_documented_doc_routes_exist_and_not_under_easy_auth(tmp_path: Path) -> None:
    local = create_app(make_settings(tmp_path))
    easy = create_app(make_settings(tmp_path, api_auth_mode=ApiAuthMode.EASY_AUTH))
    plain = lambda app: {r.path for r in app.routes if type(r) is Route}  # noqa: E731
    assert plain(local) == DOC_PATHS
    assert plain(easy) == set()
