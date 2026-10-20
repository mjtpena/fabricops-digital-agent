"""The web UI under ``/ui``: role enforcement, decisions, CSRF, XSS and response headers (ADR-0018).

The UI holds no privilege of its own. A role-less user, a reader, an operator, an administrator and an application
each get exactly what the API would give them, and an application can never approve or execute, whatever it carries.
"""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient
from httpx import Response

from orqentis.api import authz
from orqentis.api.app import create_app
from orqentis.api.roles import ROLE_AUTOMATION, ROLE_OPERATOR, ROLE_PLATFORM_ADMIN, ROLE_READER
from orqentis.bootstrap import Container, build_container
from orqentis.config.settings import ApiAuthMode, Settings
from orqentis.domain.actions import Action, ActionType
from orqentis.domain.approvals import ApprovalRequest, ApprovalStatus
from orqentis.domain.common import Environment, new_id
from orqentis.domain.principals import Principal, PrincipalKind
from orqentis.ui.security import CONTENT_SECURITY_POLICY
from tests.conftest import REPO, make_settings
from tests.integration.test_api_identity import app_api_routes, dependency_calls, route_violations
from tests.support import easy_auth as ea
from tests.support.jobs import investigate_to_completion

pytestmark = pytest.mark.security

ORIGIN = "http://testserver"
FORM = {"Content-Type": "application/x-www-form-urlencoded"}
OPERATOR = ea.header(ea.user_access_token("oid-operator", [ROLE_OPERATOR]))
ADMIN_SESSION = ea.header(ea.user_id_token("oid-admin", [ROLE_OPERATOR, ROLE_PLATFORM_ADMIN]))
ADMIN_ONLY = ea.header(ea.user_access_token("oid-admin-only", [ROLE_PLATFORM_ADMIN]))
READER = ea.header(ea.user_access_token("oid-reader", [ROLE_READER]))
NO_ROLE = ea.header(ea.user_access_token("oid-norole"))
AUTOMATION = ea.header(ea.app_token("oid-scheduler", [ROLE_AUTOMATION]))
PRIVILEGED_APP = ea.header(ea.app_token("oid-app", [ROLE_AUTOMATION, ROLE_OPERATOR, ROLE_PLATFORM_ADMIN, ROLE_READER]))
OPERATOR_APP = ea.header(ea.app_token("oid-app-operator", [ROLE_OPERATOR]))  # an app that was given a user role
AMBIGUOUS = ea.header(ea.roles_without_scp_only("oid-x", [ROLE_PLATFORM_ADMIN, ROLE_OPERATOR]))
Headers = dict[str, str]


class SpyWriteClient:
    def __init__(self) -> None:
        self.calls: list[tuple[Action, ApprovalRequest]] = []

    async def execute(self, action: Action, *, approval: ApprovalRequest) -> dict[str, Any]:
        self.calls.append((action, approval))
        return {"executed": True}


class Ui:
    """A running app with its container, helpers to reach into it, and an investigated incident."""

    def __init__(self, client: TestClient, spy: SpyWriteClient) -> None:
        self.client = client
        self.spy = spy

    @property
    def container(self) -> Container:
        container = self.client.app.state.runtime.container  # type: ignore[attr-defined]
        assert isinstance(container, Container)
        return container

    def call(self, function: Any, *args: Any, **kwargs: Any) -> Any:
        return self.client.portal.call(lambda: function(*args, **kwargs))  # type: ignore[attr-defined]

    def investigate(self) -> tuple[str, list[str]]:
        incident_id = self.client.get("/incidents", headers=OPERATOR).json()[0]["incident_id"]
        job = investigate_to_completion(self.client, incident_id, OPERATOR)
        return incident_id, job["result"]["approval_ids"]

    def status(self, approval_id: str) -> str:
        return str(self.call(self.container.approvals.get, approval_id).status.value)

    def get(self, path: str, headers: Headers, **kwargs: Any) -> Response:
        return self.client.get(path, headers=headers, **kwargs)

    def token(self, path: str, headers: Headers) -> str:
        """The CSRF token of the form on a page, as the person looking at it would send it."""
        page = self.get(path, headers)
        assert page.status_code == 200, page.text
        found = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
        assert found is not None, "the page has no form"
        return found.group(1)

    def post(
        self, path: str, headers: Headers, token: str | None, *, comment: str | None = None, origin: str | None = ORIGIN
    ) -> Response:
        """Submit a form the way a browser does: URL-encoded, with the Origin header of the page it was on."""
        fields = {"csrf_token": token} if token is not None else {}
        if comment is not None:
            fields["comment"] = comment
        extra = {"Origin": origin} if origin is not None else {}
        return self.client.post(
            path, content=urlencode(fields), headers={**headers, **FORM, **extra}, follow_redirects=False
        )

    def forged_token(self, oid: str) -> str:
        """A genuine token for ``oid``, as if that person had loaded another page (their own page has no form)."""
        return str(self.client.app.state.ui.csrf.issue(oid))  # type: ignore[attr-defined]

    def request_as_human(self, requester_oid: str, incident_id: str) -> str:
        """An approval that a person (not the agent) requested, as when someone proposes an action by hand."""
        container = self.container
        requester = Principal(id=requester_oid, display_name="Requester", kind=PrincipalKind.HUMAN)
        action = Action(
            action_id=new_id("ACT"),
            incident_id=incident_id,
            action_type=ActionType.RERUN_PIPELINE,
            title="Re-run the pipeline",
            target_item_id="pl-ingest-sales-prod",
            target_display_name="ingest_sales_prod",
            workspace_id="ws-sales-prod",
            environment=Environment.PROD,
            parameters={"reason": "requested by hand"},
            requested_by=requester.id,
            created_at=container.clock.now(),
        )
        incident = self.call(container.incidents.get, incident_id)
        outcome = self.call(container.gateway.submit, action, principal=requester, incident=incident)
        assert outcome.approval is not None
        return str(outcome.approval.approval_id)


def build_ui(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spy: SpyWriteClient, **overrides: object) -> TestClient:
    values: dict[str, object] = {"api_auth_mode": ApiAuthMode.EASY_AUTH, "enable_write_actions": True}
    values.update(overrides)
    settings = make_settings(tmp_path, **values)

    async def build_with_spy(s: Settings) -> Container:
        return await build_container(s, write_client=spy)

    monkeypatch.setattr("orqentis.api.app.build_container", build_with_spy)
    return TestClient(create_app(settings))


@pytest.fixture
def ui(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Ui]:
    spy = SpyWriteClient()
    with build_ui(tmp_path, monkeypatch, spy) as client:
        yield Ui(client, spy)


def is_html(response: Response) -> bool:
    return response.headers["content-type"].startswith("text/html")


# ------------------------------------------------------------------------------------ who sees which page


def pages(incident_id: str, approval_id: str) -> list[str]:
    return [
        "/ui/incidents",
        "/ui/incidents?status=DIAGNOSED&severity=HIGH",
        f"/ui/incidents/{incident_id}",
        f"/ui/incidents/{incident_id}/report",
        "/ui/approvals",
        "/ui/approvals?status=PENDING",
        f"/ui/approvals/{approval_id}",
        "/ui/audit",
        "/ui/assets/ui.css",
    ]


@pytest.mark.parametrize(
    "caller",
    [READER, AUTOMATION, OPERATOR, ADMIN_SESSION, ADMIN_ONLY],
    ids=["reader", "automation-app", "operator", "admin-id-token-session", "admin-only"],
)
def test_everyone_with_a_fabricops_role_can_read_every_page(ui: Ui, caller: Headers) -> None:
    incident_id, approvals = ui.investigate()
    for path in pages(incident_id, approvals[0]):
        response = ui.get(path, caller)
        assert response.status_code == 200, (path, response.status_code)
    assert ui.get("/ui", caller).status_code == 200  # follows the redirect to the incident list
    redirect = ui.client.get("/ui", headers=caller, follow_redirects=False)
    assert redirect.status_code == 303 and redirect.headers["location"] == "/ui/incidents"


@pytest.mark.parametrize(
    ("caller", "code"),
    [(NO_ROLE, "ROLE_REQUIRED"), (AMBIGUOUS, "PRINCIPAL_AMBIGUOUS")],
    ids=["no-role", "ambiguous-claims"],
)
def test_a_caller_without_a_usable_role_gets_nothing(ui: Ui, caller: Headers, code: str) -> None:
    incident_id, approvals = ui.investigate()
    for path in ["/ui", *pages(incident_id, approvals[0])]:
        response = ui.client.get(path, headers=caller, follow_redirects=False)
        assert response.status_code == 403, path
        assert is_html(response) and code in response.text  # an error page, not JSON and not the data
        assert incident_id not in response.text and approvals[0] not in response.text


def test_without_an_identity_the_ui_is_401(ui: Ui) -> None:
    for path in ("/ui", "/ui/incidents", "/ui/approvals", "/ui/audit", "/ui/assets/ui.css"):
        assert ui.client.get(path, follow_redirects=False).status_code == 401, path
    assert ui.client.get("/health").status_code == 200  # still the only anonymous route


def test_a_reader_sees_the_approval_but_no_decision_controls(ui: Ui) -> None:
    _, approvals = ui.investigate()
    page = ui.get(f"/ui/approvals/{approvals[0]}", READER).text
    assert "csrf_token" not in page and "Approve" not in page and "Reject" not in page
    assert "Only a person who holds an approver role" in page


def test_an_application_sees_no_decision_controls_even_with_every_role(ui: Ui) -> None:
    _, approvals = ui.investigate()
    for caller in (AUTOMATION, PRIVILEGED_APP, OPERATOR_APP):
        page = ui.get(f"/ui/approvals/{approvals[0]}", caller)
        assert page.status_code == 200
        assert "csrf_token" not in page.text and "Approve" not in page.text and "Execute" not in page.text


def test_an_operator_sees_the_decision_form_with_a_token_bound_to_their_identity(ui: Ui) -> None:
    _, approvals = ui.investigate()
    page = ui.get(f"/ui/approvals/{approvals[0]}", OPERATOR).text
    assert 'name="csrf_token"' in page and ">Approve</button>" in page and ">Reject</button>" in page
    assert 'method="post" action="/ui/approvals/' in page and 'maxlength="1000"' in page


# ------------------------------------------------------------------------------- decisions through the UI


def test_an_operator_approves_through_the_ui_with_the_same_effects_as_the_api(ui: Ui) -> None:
    _, approvals = ui.investigate()
    path = f"/ui/approvals/{approvals[0]}"
    response = ui.post(f"{path}/approve", OPERATOR, ui.token(path, OPERATOR), comment="Looks right to me")
    assert response.status_code == 303 and response.headers["location"] == f"{path}?done=approved"
    approval = ui.call(ui.container.approvals.get, approvals[0])
    assert approval.status is ApprovalStatus.APPROVED and approval.approved_by == "oid-operator"
    assert approval.decision_comment == "Looks right to me"
    page = ui.get(response.headers["location"], OPERATOR).text
    assert "The request was approved." in page and "Looks right to me" in page
    audit = ui.get("/audit", OPERATOR, params={"limit": 5000}).json()
    decided = [r for r in audit if r["action"] == "approval.decide" and r["outcome"] == "APPROVED"]
    assert decided and decided[-1]["actor"] == "oid-operator"  # recorded against the signed-in person


def test_an_id_token_browser_session_can_reject_with_a_comment(ui: Ui) -> None:
    _, approvals = ui.investigate()
    path = f"/ui/approvals/{approvals[1]}"
    response = ui.post(f"{path}/reject", ADMIN_SESSION, ui.token(path, ADMIN_SESSION), comment="Not during the close")
    assert response.status_code == 303 and response.headers["location"].endswith("done=rejected")
    assert ui.status(approvals[1]) == "REJECTED" and ui.status(approvals[0]) == "PENDING"


def test_execution_through_the_ui_runs_once_and_only_after_approval(ui: Ui) -> None:
    _, approvals = ui.investigate()
    path = f"/ui/approvals/{approvals[0]}"
    assert "Execute the approved action" not in ui.get(path, ADMIN_SESSION).text  # nothing to execute yet
    refused = ui.post(f"{path}/execute", ADMIN_SESSION, ui.token(path, ADMIN_SESSION))
    assert refused.status_code == 403 and "ACTION_REQUIRES_APPROVAL" in refused.text and ui.spy.calls == []

    ui.post(f"{path}/approve", OPERATOR, ui.token(path, OPERATOR))
    assert "Execute the approved action" in ui.get(path, ADMIN_SESSION).text
    done = ui.post(f"{path}/execute", ADMIN_SESSION, ui.token(path, ADMIN_SESSION))
    assert done.status_code == 303 and done.headers["location"].endswith("done=executed")
    assert len(ui.spy.calls) == 1
    again = ui.post(f"{path}/execute", ADMIN_SESSION, ui.forged_token("oid-admin"))  # the page has no form now
    assert again.status_code == 403 and "ALREADY_EXECUTED" in again.text and len(ui.spy.calls) == 1
    assert "Execute the approved action" not in ui.get(path, ADMIN_SESSION).text  # single use


@pytest.mark.parametrize("verb", ["approve", "reject", "execute"])
@pytest.mark.parametrize(
    ("caller", "code"),
    [
        (NO_ROLE, "ROLE_REQUIRED"),
        (READER, "ROLE_REQUIRED"),
        (AUTOMATION, "PRINCIPAL_NOT_HUMAN"),
        (PRIVILEGED_APP, "PRINCIPAL_NOT_HUMAN"),
        (OPERATOR_APP, "PRINCIPAL_NOT_HUMAN"),
        (AMBIGUOUS, "PRINCIPAL_AMBIGUOUS"),
    ],
    ids=["no-role", "reader", "automation-app", "app-with-every-role", "app-with-operator-role", "ambiguous"],
)
def test_nobody_but_a_human_approver_can_decide_or_execute_through_the_ui(
    ui: Ui, caller: Headers, code: str, verb: str
) -> None:
    _, approvals = ui.investigate()
    path = f"/ui/approvals/{approvals[0]}"
    # Even holding a perfectly valid form token (issued to an operator's page) and the right Origin.
    token = ui.token(path, OPERATOR)
    if verb == "execute":
        ui.post(
            f"{path}/approve", OPERATOR, token
        )  # approved: only the identity checks stand between the app and a write
    response = ui.post(f"{path}/{verb}", caller, token)
    assert response.status_code == 403 and code in response.text, response.text
    assert ui.status(approvals[0]) == ("APPROVED" if verb == "execute" else "PENDING")
    assert ui.spy.calls == []


def test_an_approval_the_user_requested_themselves_cannot_be_decided_by_them(ui: Ui) -> None:
    incident_id, _ = ui.investigate()
    own = ui.request_as_human("oid-operator", incident_id)
    path = f"/ui/approvals/{own}"
    page = ui.get(path, OPERATOR).text
    assert "You requested this approval, so you cannot decide it" in page and "csrf_token" not in page
    # Forcing the request anyway (a token from another page of theirs) is refused by the approval service.
    response = ui.post(f"{path}/approve", OPERATOR, ui.forged_token("oid-operator"))
    assert response.status_code == 403 and "APPROVAL_FORBIDDEN" in response.text and "own request" in response.text
    assert ui.status(own) == "PENDING"
    # A different operator can.
    assert ui.post(f"{path}/approve", ADMIN_SESSION, ui.token(path, ADMIN_SESSION)).status_code == 303
    assert ui.status(own) == "APPROVED"


def test_a_human_without_the_role_the_approval_needs_is_refused_by_the_service(ui: Ui) -> None:
    _, approvals = ui.investigate()
    path = f"/ui/approvals/{approvals[0]}"
    page = ui.get(path, ADMIN_ONLY).text
    assert "needs the role" in page and "csrf_token" not in page  # not offered ...
    response = ui.post(f"{path}/approve", ADMIN_ONLY, ui.forged_token("oid-admin-only"))  # ... and refused when forced
    assert response.status_code == 403 and "APPROVAL_FORBIDDEN" in response.text
    assert ui.status(approvals[0]) == "PENDING"


def test_deciding_twice_or_after_the_fact_shows_the_conflict_as_a_page(ui: Ui) -> None:
    _, approvals = ui.investigate()
    path = f"/ui/approvals/{approvals[0]}"
    assert ui.post(f"{path}/approve", OPERATOR, ui.token(path, OPERATOR)).status_code == 303
    token = ui.forged_token("oid-operator")
    second = ui.post(f"{path}/reject", OPERATOR, token)
    assert second.status_code == 409 and is_html(second) and "APPROVAL_NOT_PENDING" in second.text
    missing = ui.post("/ui/approvals/APR-does-not-exist/approve", OPERATOR, token)
    assert missing.status_code == 404 and "APPROVAL_NOT_FOUND" in missing.text


def test_a_comment_over_the_api_limit_is_refused(ui: Ui) -> None:
    _, approvals = ui.investigate()
    path = f"/ui/approvals/{approvals[0]}"
    response = ui.post(f"{path}/approve", OPERATOR, ui.token(path, OPERATOR), comment="x" * 1001)
    assert response.status_code == 422 and "INVALID_COMMENT" in response.text and ui.status(approvals[0]) == "PENDING"


def test_unknown_things_are_404_pages_and_bad_filters_400(ui: Ui) -> None:
    assert ui.get("/ui/incidents/INC-0000-0000", READER).status_code == 404
    assert ui.get("/ui/approvals/APR-nope", READER).status_code == 404
    bad = ui.get("/ui/incidents?status=bogus", READER)
    assert bad.status_code == 400 and is_html(bad) and "INVALID_FILTER" in bad.text
    assert ui.get("/ui/approvals?status=bogus", READER).status_code == 400
    assert ui.get("/ui/incidents?status=&severity=", READER).status_code == 200  # "All" in the form is empty


def test_the_incident_list_filters_by_status_and_severity(ui: Ui) -> None:
    incident_id, _ = ui.investigate()
    found = ui.get("/ui/incidents?status=AWAITING_APPROVAL&severity=high", READER).text
    assert incident_id in found
    for query in ("status=RESOLVED", "severity=CRITICAL", "status=AWAITING_APPROVAL&severity=LOW"):
        assert incident_id not in ui.get(f"/ui/incidents?{query}", READER).text, query


def test_the_report_labels_every_statement_and_cites_evidence_ids(ui: Ui) -> None:
    incident_id, _ = ui.investigate()
    html = ui.get(f"/ui/incidents/{incident_id}/report", READER).text
    for label in ("FACT", "INFERENCE", "RECOMMENDATION"):
        assert f'class="badge kind-{label}">{label}<' in html
    assert re.search(rf"<code>{incident_id}-[A-Za-z0-9-]+</code>", html)  # evidence ids are shown
    for title in (
        "Executive Summary",
        "Probable Root Cause",
        "Confidence",
        "Recommended Remediation",
        "Approval Requirement",
    ):
        assert f"<h2>{title}</h2>" in html


def test_the_audit_page_verifies_the_chain_and_reports_tampering(ui: Ui, tmp_path: Path) -> None:
    ui.investigate()
    ok = ui.get("/ui/audit", READER).text
    assert "VALID" in ok and "hash chain" in ok and "INVALID" not in ok
    audit_file = tmp_path / "var" / "audit" / "audit.jsonl"
    lines = audit_file.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[3])
    record["outcome"] = "TAMPERED"
    lines[3] = json.dumps(record)
    audit_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    bad = ui.get("/ui/audit", READER).text
    assert "INVALID" in bad and "hash chain broken" in bad


# ------------------------------------------------------------------------------------------------- CSRF


@pytest.fixture
def pending(ui: Ui) -> tuple[Ui, str, str]:
    _, approvals = ui.investigate()
    return ui, f"/ui/approvals/{approvals[0]}", approvals[0]


def assert_untouched(ui: Ui, approval_id: str) -> None:
    assert ui.status(approval_id) == "PENDING" and ui.spy.calls == []
    refusals = [r for r in ui.call(ui.container.audit.list) if r.action == "approval.decide"]
    assert refusals == []  # the service was never reached


def test_a_missing_token_is_refused(pending: tuple[Ui, str, str]) -> None:
    ui, path, approval_id = pending
    for token in (None, "", "   "):
        response = ui.post(f"{path}/approve", OPERATOR, token)
        assert response.status_code == 403 and "CSRF_TOKEN_INVALID" in response.text, token
    assert_untouched(ui, approval_id)


@pytest.mark.parametrize(
    "token",
    [
        "not-a-token",
        "v1.1.2.3",
        "v1." + "9" * 12 + ".abc.def",
        "v2.9999999999.abc.def",
        "x" * 5000,
        "v1.9999999999.é.é",
    ],
)
def test_a_wrong_or_malformed_token_is_refused(pending: tuple[Ui, str, str], token: str) -> None:
    ui, path, approval_id = pending
    response = ui.post(f"{path}/approve", OPERATOR, token)
    assert response.status_code == 403 and "CSRF_TOKEN_INVALID" in response.text
    assert_untouched(ui, approval_id)


def test_a_token_issued_to_somebody_else_is_refused(pending: tuple[Ui, str, str]) -> None:
    ui, path, approval_id = pending
    stolen = ui.token(path, ADMIN_SESSION)  # a real, valid token ... for another principal
    response = ui.post(f"{path}/approve", OPERATOR, stolen)
    assert response.status_code == 403 and "CSRF_TOKEN_INVALID" in response.text
    assert_untouched(ui, approval_id)
    # The same token works for the person it was issued to.
    assert ui.post(f"{path}/approve", ADMIN_SESSION, stolen).status_code == 303


def test_a_tampered_token_is_refused(pending: tuple[Ui, str, str]) -> None:
    ui, path, approval_id = pending
    token = ui.token(path, OPERATOR)
    version, expiry, nonce, mac = token.split(".")
    forged = [
        f"{version}.{int(expiry) + 3600}.{nonce}.{mac}",  # a longer life
        f"{version}.{expiry}.{nonce}x.{mac}",
        f"{version}.{expiry}.{nonce}.{mac[:-2]}AA",
    ]
    for candidate in forged:
        assert ui.post(f"{path}/approve", OPERATOR, candidate).status_code == 403
    assert_untouched(ui, approval_id)


def test_an_expired_token_is_refused(pending: tuple[Ui, str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    ui, path, approval_id = pending
    token = ui.token(path, OPERATOR)
    real = ui.client.app.state.ui.csrf._clock  # type: ignore[attr-defined]
    monkeypatch.setattr(ui.client.app.state.ui.csrf, "_clock", lambda: real() + 5 * 3600)  # type: ignore[attr-defined]
    response = ui.post(f"{path}/approve", OPERATOR, token)
    assert response.status_code == 403 and "CSRF_TOKEN_INVALID" in response.text
    assert_untouched(ui, approval_id)


@pytest.mark.parametrize(
    "origin",
    [
        "https://evil.example",
        "http://evil.example",
        "http://testserver.evil.example",
        "http://evil.example/testserver",
        "https://testserver",  # same host, other scheme
        "http://testserver:8080",  # same host, other port
        "null",
        "",
        "not an origin",
    ],
)
def test_a_foreign_origin_is_refused(pending: tuple[Ui, str, str], origin: str) -> None:
    ui, path, approval_id = pending
    token = ui.token(path, OPERATOR)  # even with a genuine token (as if it had leaked)
    for verb in ("approve", "reject", "execute"):
        response = ui.post(f"{path}/{verb}", OPERATOR, token, origin=origin)
        assert response.status_code == 403 and "CROSS_ORIGIN_REQUEST" in response.text, (verb, origin)
    assert_untouched(ui, approval_id)


def test_a_foreign_referer_is_refused_and_a_matching_one_accepted_when_there_is_no_origin(
    pending: tuple[Ui, str, str],
) -> None:
    ui, path, approval_id = pending
    token = ui.token(path, OPERATOR)
    foreign = ui.client.post(
        f"{path}/approve",
        data={"csrf_token": token},
        headers={**OPERATOR, "Referer": "https://evil.example/attack.html"},
        follow_redirects=False,
    )
    assert foreign.status_code == 403 and "CROSS_ORIGIN_REQUEST" in foreign.text
    assert_untouched(ui, approval_id)
    own = ui.client.post(
        f"{path}/approve",
        data={"csrf_token": token},
        headers={**OPERATOR, "Referer": f"{ORIGIN}{path}"},
        follow_redirects=False,
    )
    assert own.status_code == 303


def test_a_ui_post_with_neither_origin_nor_referer_is_refused(pending: tuple[Ui, str, str]) -> None:
    ui, path, approval_id = pending
    response = ui.post(f"{path}/approve", OPERATOR, ui.token(path, OPERATOR), origin=None)
    assert response.status_code == 403 and "ORIGIN_REQUIRED" in response.text
    assert_untouched(ui, approval_id)


@pytest.mark.parametrize("site", ["cross-site", "same-site", "bogus"])
def test_fetch_metadata_from_another_site_is_refused_even_with_a_matching_origin(
    pending: tuple[Ui, str, str], site: str
) -> None:
    ui, path, approval_id = pending
    response = ui.client.post(
        f"{path}/approve",
        data={"csrf_token": ui.token(path, OPERATOR)},
        headers={**OPERATOR, "Origin": ORIGIN, "Sec-Fetch-Site": site},
        follow_redirects=False,
    )
    assert response.status_code == 403 and "CROSS_SITE_REQUEST" in response.text
    assert_untouched(ui, approval_id)


def test_same_origin_fetch_metadata_is_accepted(pending: tuple[Ui, str, str]) -> None:
    ui, path, _ = pending
    response = ui.client.post(
        f"{path}/approve",
        data={"csrf_token": ui.token(path, OPERATOR)},
        headers={**OPERATOR, "Origin": ORIGIN, "Sec-Fetch-Site": "same-origin"},
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_forms_must_be_url_encoded_and_must_not_repeat_fields(pending: tuple[Ui, str, str]) -> None:
    ui, path, approval_id = pending
    token = ui.token(path, OPERATOR)
    base = {**OPERATOR, "Origin": ORIGIN}
    json_post = ui.client.post(f"{path}/approve", json={"csrf_token": token}, headers=base, follow_redirects=False)
    assert json_post.status_code == 415 and "UNSUPPORTED_MEDIA_TYPE" in json_post.text
    multipart = ui.client.post(
        f"{path}/approve", files={"csrf_token": (None, token)}, headers=base, follow_redirects=False
    )
    assert multipart.status_code == 415
    no_body = ui.client.post(f"{path}/approve", headers=base, follow_redirects=False)
    assert no_body.status_code == 415
    twice = ui.client.post(
        f"{path}/approve",
        content=f"csrf_token={token}&csrf_token={token}",
        headers={**base, **FORM},
        follow_redirects=False,
    )
    assert twice.status_code == 400 and "BAD_FORM" in twice.text
    garbled = ui.client.post(
        f"{path}/approve", content=b"csrf_token=\xff\xfe", headers={**base, **FORM}, follow_redirects=False
    )
    assert garbled.status_code == 400
    assert_untouched(ui, approval_id)


def test_a_token_in_the_query_string_or_a_header_does_not_count(pending: tuple[Ui, str, str]) -> None:
    ui, path, approval_id = pending
    token = ui.token(path, OPERATOR)
    response = ui.client.post(
        f"{path}/approve?csrf_token={token}",
        content=b"comment=hi",
        headers={**OPERATOR, **FORM, "Origin": ORIGIN, "X-CSRF-Token": token},
        follow_redirects=False,
    )
    assert response.status_code == 403 and "CSRF_TOKEN_INVALID" in response.text
    assert_untouched(ui, approval_id)


def test_csrf_refusals_are_audited_against_the_signed_in_person_without_flooding_the_log(
    pending: tuple[Ui, str, str],
) -> None:
    ui, path, _ = pending
    for _ in range(40):
        ui.post(f"{path}/approve", OPERATOR, "wrong")
    refused = [
        r
        for r in ui.get("/audit", OPERATOR, params={"limit": 5000}).json()
        if r["details"].get("code") == "CSRF_TOKEN_INVALID"
    ]
    assert 1 <= len(refused) <= 20 and refused[0]["actor"] == "oid-operator"
    assert ui.get("/audit/verify", OPERATOR).json()["valid"] is True


def test_the_csrf_key_can_be_configured_so_that_tokens_survive_a_restart_and_work_across_replicas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = SpyWriteClient()
    with build_ui(tmp_path / "one", monkeypatch, spy, ui_csrf_secret="shared-key-from-key-vault") as one:
        first = Ui(one, spy)
        _, approvals = first.investigate()
        token = first.token(f"/ui/approvals/{approvals[0]}", OPERATOR)
    with build_ui(tmp_path / "two", monkeypatch, spy, ui_csrf_secret="shared-key-from-key-vault") as two:
        assert two.app.state.ui.csrf.verify(token, "oid-operator")  # type: ignore[attr-defined]
    with build_ui(tmp_path / "three", monkeypatch, spy) as three:  # no shared key: a random one per process
        assert not three.app.state.ui.csrf.verify(token, "oid-operator")  # type: ignore[attr-defined]


# ----------------------------------------------------------------------- the API is not a CSRF target either


@pytest.mark.parametrize("verb", ["approve", "reject", "execute"])
def test_the_api_refuses_a_cross_origin_browser_post_even_for_a_signed_in_human(ui: Ui, verb: str) -> None:
    """The ID-token session cookie makes a browser HUMAN: without this a foreign page could POST ``/approve``."""
    _, approvals = ui.investigate()
    for headers in (
        {"Origin": "https://evil.example"},
        {"Origin": "null"},
        {"Sec-Fetch-Site": "cross-site"},
        {"Origin": ORIGIN, "Sec-Fetch-Site": "same-site"},
    ):
        response = ui.client.post(f"/approvals/{approvals[0]}/{verb}", headers={**ADMIN_SESSION, **headers})
        assert response.status_code == 403, (headers, response.text)
        assert response.json()["error"] in {"CROSS_ORIGIN_REQUEST", "CROSS_SITE_REQUEST"}
    assert ui.status(approvals[0]) == "PENDING" and ui.spy.calls == []


def test_the_api_refuses_cross_origin_investigation_and_collection(ui: Ui) -> None:
    incident_id = ui.client.get("/incidents", headers=OPERATOR).json()[0]["incident_id"]
    for path in (f"/incidents/{incident_id}/investigate", "/telemetry/collect"):
        assert ui.client.post(path, headers={**OPERATOR, "Origin": "https://evil.example"}).status_code == 403


def test_the_api_refuses_html_form_bodies_and_still_serves_json_and_scripts(ui: Ui) -> None:
    _, approvals = ui.investigate()
    url = f"/approvals/{approvals[0]}/approve"
    for content_type in ("application/x-www-form-urlencoded", "multipart/form-data; boundary=x", "text/plain"):
        response = ui.client.post(url, content=b"comment=hi", headers={**OPERATOR, "Content-Type": content_type})
        assert response.status_code == 415 and response.json()["error"] == "UNSUPPORTED_MEDIA_TYPE", content_type
    assert ui.status(approvals[0]) == "PENDING"
    # Same-origin JSON (the interactive docs, a script, curl with or without Origin) keeps working.
    assert ui.client.post(url, json={"comment": "ok"}, headers={**OPERATOR, "Origin": ORIGIN}).status_code == 200
    _, more = approvals[0], approvals[1]
    assert ui.client.post(f"/approvals/{more}/reject", headers=ADMIN_SESSION).status_code == 200  # no body, no Origin


def test_reads_are_never_blocked_by_the_guard(ui: Ui) -> None:
    assert ui.client.get("/incidents", headers={**OPERATOR, "Origin": "https://evil.example"}).status_code == 200
    assert ui.client.get("/ui/incidents", headers={**OPERATOR, "Sec-Fetch-Site": "cross-site"}).status_code == 200


def test_behind_the_ingress_the_forwarded_scheme_decides_which_origin_is_own(ui: Ui) -> None:
    _, approvals = ui.investigate()
    url = f"/approvals/{approvals[0]}/approve"
    headers = {**OPERATOR, "Origin": "https://testserver"}
    assert ui.client.post(url, headers=headers).status_code == 403  # the request itself arrived as http
    assert ui.client.post(url, headers={**headers, "X-Forwarded-Proto": "https"}).status_code == 200


def test_the_host_a_proxy_forwarded_is_an_own_origin_but_a_foreign_one_is_not(ui: Ui) -> None:
    _, approvals = ui.investigate()
    url = f"/approvals/{approvals[0]}/approve"
    proxied = {**OPERATOR, "Origin": "https://fabricops.contoso.com", "X-Forwarded-Proto": "https"}
    assert ui.client.post(url, headers=proxied).status_code == 403  # nothing says this host is ours
    assert ui.client.post(url, headers={**proxied, "X-Forwarded-Host": "evil.example"}).status_code == 403
    assert ui.status(approvals[0]) == "PENDING"
    assert ui.client.post(url, headers={**proxied, "X-Forwarded-Host": "fabricops.contoso.com"}).status_code == 200


def test_the_configured_public_base_url_is_an_own_origin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spy = SpyWriteClient()
    with build_ui(tmp_path, monkeypatch, spy, public_base_url="https://fabricops.contoso.com") as client:
        ui = Ui(client, spy)
        _, approvals = ui.investigate()
        url = f"/approvals/{approvals[0]}/approve"
        assert client.post(url, headers={**OPERATOR, "Origin": "https://evil.example"}).status_code == 403
        assert client.post(url, headers={**OPERATOR, "Origin": "https://fabricops.contoso.com"}).status_code == 200


def test_the_guard_also_protects_the_local_dev_headers_mode(tmp_path: Path) -> None:
    with TestClient(create_app(make_settings(tmp_path))) as client:
        headers = {"X-Orqentis-User": "olivia@contoso.com", "X-Orqentis-Roles": ROLE_OPERATOR}
        assert (
            client.post("/telemetry/collect", headers={**headers, "Origin": "https://evil.example"}).status_code == 403
        )
        assert client.post("/telemetry/collect", headers=headers).status_code == 200


# --------------------------------------------------------------------------------------------- XSS


HOSTILE = '<script>alert("xss")</script><img src=x onerror=alert(1)>'


@pytest.fixture
def hostile_ui(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Ui]:
    """The estate's pipeline carries script markup in its description, as a malicious Fabric author could write."""
    fixtures = tmp_path / "fixtures"
    shutil.copytree(REPO / "fixtures", fixtures)
    estate_path = fixtures / "fabric" / "estate.json"
    estate = json.loads(estate_path.read_text(encoding="utf-8"))
    item = next(i for i in estate["items"] if i["id"] == "pl-ingest-sales-prod")
    item["description"] = HOSTILE
    estate_path.write_text(json.dumps(estate), encoding="utf-8")
    spy = SpyWriteClient()
    with build_ui(tmp_path, monkeypatch, spy, fixtures_dir=fixtures) as client:
        yield Ui(client, spy)


def assert_inert(html: str) -> None:
    assert "<script" not in html.lower() and "<img" not in html.lower() and "<svg" not in html.lower()
    assert re.search(r"<[a-z][^>]*\sonerror\s*=", html, re.I) is None


def test_script_markup_in_a_fabric_item_description_renders_as_text(hostile_ui: Ui) -> None:
    incident_id, approvals = hostile_ui.investigate()
    seen = 0
    for path in (
        f"/ui/incidents/{incident_id}",
        f"/ui/incidents/{incident_id}/report",
        f"/ui/approvals/{approvals[0]}",
    ):
        html = hostile_ui.get(path, OPERATOR).text
        assert_inert(html)
        seen += html.count("&lt;script&gt;")
    assert seen >= 1, "the description never reached a page, so this test proved nothing"
    detail = hostile_ui.get(f"/ui/incidents/{incident_id}", OPERATOR).text
    assert "onerror=alert(1)" in detail  # visible as text ...
    assert "&lt;img src=x onerror=alert(1)&gt;" in detail  # ... escaped, not markup


def test_every_untrusted_field_of_an_incident_and_an_approval_is_escaped(hostile_ui: Ui) -> None:
    incident_id, approvals = hostile_ui.investigate()
    container = hostile_ui.container
    incident = hostile_ui.call(container.incidents.get, incident_id)
    incident.title = f"Pipeline {HOSTILE} failed"
    incident.primary_item_name = HOSTILE
    incident.symptoms[0].description = HOSTILE
    incident.timeline[0].summary = HOSTILE
    incident.timeline[-1].actor = HOSTILE
    incident.evidence[0].summary = HOSTILE
    incident.evidence[0].structured_payload = {"message": HOSTILE, HOSTILE: HOSTILE}
    incident.insufficient_evidence_reason = HOSTILE
    incident.resolution_summary = HOSTILE
    hostile_ui.call(container.incidents.repository.save, incident)
    approval = hostile_ui.call(container.approvals.get, approvals[0])
    action = approval.proposed_action.model_copy(
        update={"title": HOSTILE, "description": HOSTILE, "target_display_name": HOSTILE, "parameters": {"k": HOSTILE}}
    )
    hostile_ui.call(
        container.approvals.save, approval.model_copy(update={"proposed_action": action, "reason": HOSTILE})
    )
    for path in (
        "/ui/incidents",
        f"/ui/incidents/{incident_id}",
        f"/ui/incidents/{incident_id}/report",
        "/ui/approvals",
        f"/ui/approvals/{approvals[0]}",
        "/ui/audit",
    ):
        html = hostile_ui.get(path, OPERATOR).text
        assert_inert(html)
        assert "&lt;script&gt;" in html or path == "/ui/audit", path


def test_a_hostile_comment_and_display_name_are_escaped(hostile_ui: Ui) -> None:
    _, approvals = hostile_ui.investigate()
    path = f"/ui/approvals/{approvals[0]}"
    sneaky = ea.header(ea.user_access_token("oid-operator", [ROLE_OPERATOR], name=HOSTILE))
    token = hostile_ui.token(path, sneaky)
    hostile_ui.post(f"{path}/approve", sneaky, token, comment=HOSTILE)
    html = hostile_ui.get(path, sneaky).text
    assert_inert(html)
    assert html.count("&lt;script&gt;") >= 2  # the comment and the name in the header


def test_an_error_page_does_not_reflect_markup(hostile_ui: Ui) -> None:
    response = hostile_ui.get(f"/ui/incidents/{HOSTILE}", OPERATOR)
    assert response.status_code == 404 and is_html(response)
    assert_inert(response.text)
    bad_filter = hostile_ui.get("/ui/incidents", OPERATOR, params={"status": HOSTILE})
    assert bad_filter.status_code == 400
    assert_inert(bad_filter.text)


# --------------------------------------------------------------------------------------------- headers


def test_every_ui_response_carries_the_security_headers(ui: Ui) -> None:
    incident_id, approvals = ui.investigate()
    path = f"/ui/approvals/{approvals[0]}"
    token = ui.token(path, OPERATOR)
    responses = [
        ui.get("/ui/incidents", READER),
        ui.client.get("/ui", headers=READER, follow_redirects=False),
        ui.get(f"/ui/incidents/{incident_id}/report", READER),
        ui.get("/ui/assets/ui.css", READER),
        ui.get("/ui/incidents", NO_ROLE),  # 403
        ui.get("/ui/incidents/INC-nope", READER),  # 404
        ui.get("/ui/incidents?status=bogus", READER),  # 400
        ui.client.get("/ui/incidents"),  # 401 from the identity check
        ui.post(f"{path}/approve", OPERATOR, "wrong"),  # 403 from the CSRF check
        ui.post(f"{path}/approve", OPERATOR, token, origin="https://evil.example"),  # 403 from the guard
        ui.client.post(f"{path}/approve", content=b"x" * 70_000, headers={**OPERATOR, **FORM, "Origin": ORIGIN}),  # 413
        ui.post(f"{path}/approve", OPERATOR, token),  # 303
    ]
    assert {r.status_code for r in responses} >= {200, 303, 400, 401, 403, 404, 413}
    for response in responses:
        headers = response.headers
        assert headers["content-security-policy"] == CONTENT_SECURITY_POLICY
        assert headers["x-frame-options"] == "DENY" and headers["x-content-type-options"] == "nosniff"
        assert headers["referrer-policy"] == "same-origin"
        assert headers["cross-origin-opener-policy"] == "same-origin"
    assert ui.get("/ui/incidents", READER).headers["cache-control"] == "no-store"
    assert "max-age" in ui.get("/ui/assets/ui.css", READER).headers["cache-control"]
    assert ui.get("/ui/assets/ui.css", READER).headers["content-type"].startswith("text/css")


def test_the_content_security_policy_allows_no_script_and_no_foreign_resource() -> None:
    policy = {p.split()[0]: p.split()[1:] for p in CONTENT_SECURITY_POLICY.split("; ")}
    assert policy["default-src"] == ["'none'"] and "script-src" not in policy  # nothing may run
    assert policy["style-src"] == ["'self'"] and policy["form-action"] == ["'self'"]
    assert policy["frame-ancestors"] == ["'none'"] and policy["base-uri"] == ["'none'"]
    assert "unsafe-inline" not in CONTENT_SECURITY_POLICY and "unsafe-eval" not in CONTENT_SECURITY_POLICY
    assert "*" not in CONTENT_SECURITY_POLICY and "http" not in CONTENT_SECURITY_POLICY


def test_api_responses_keep_their_own_headers(ui: Ui) -> None:
    response = ui.client.get("/incidents", headers=OPERATOR)
    assert "content-security-policy" not in response.headers  # the CSP is for pages, not for JSON


def test_hsts_is_sent_when_the_request_came_over_https(ui: Ui) -> None:
    secure = ui.get("/ui/incidents", {**READER, "X-Forwarded-Proto": "https"})
    assert secure.headers["strict-transport-security"].startswith("max-age=")
    assert "strict-transport-security" not in ui.get("/ui/incidents", READER).headers


def test_templates_contain_no_script_inline_style_event_handler_or_safe_filter() -> None:
    templates = sorted((REPO / "src" / "orqentis" / "ui" / "templates").glob("*.html"))
    assert len(templates) >= 8
    for template in templates:
        text = template.read_text(encoding="utf-8")
        assert "|safe" not in text and "Markup" not in text and "autoescape" not in text, template.name
        assert not re.search(r"<script|<iframe|<object|<embed|javascript:", text, re.I), template.name
        assert not re.search(r"\sstyle\s*=|\son[a-z]+\s*=", text, re.I), template.name
        assert not re.search(r"""(src|href|action)\s*=\s*["']?https?:""", text, re.I), template.name


# ---------------------------------------------------------------------------- structure of the UI routes


def test_every_ui_route_is_authenticated_role_guarded_and_every_post_is_human_only(tmp_path: Path) -> None:
    for mode in (ApiAuthMode.DEV_HEADERS, ApiAuthMode.EASY_AUTH):
        app = create_app(make_settings(tmp_path, api_auth_mode=mode))
        assert route_violations(app) == []
        ui_routes = [r for r in app_api_routes(app) if r.path.startswith("/ui")]
        assert {r.path for r in ui_routes} == {  # the walk above really looked at exactly these
            "/ui",
            "/ui/assets/ui.css",
            "/ui/incidents",
            "/ui/incidents/{incident_id}",
            "/ui/incidents/{incident_id}/report",
            "/ui/approvals",
            "/ui/approvals/{approval_id}",
            "/ui/approvals/{approval_id}/approve",
            "/ui/approvals/{approval_id}/reject",
            "/ui/approvals/{approval_id}/execute",
            "/ui/audit",
        }
        posts = [r for r in ui_routes if "POST" in r.methods]
        assert {r.path for r in posts} == {
            "/ui/approvals/{approval_id}/approve",
            "/ui/approvals/{approval_id}/reject",
            "/ui/approvals/{approval_id}/execute",
        }
        for route in posts:
            assert {authz.require_human, authz.require_decider} <= dependency_calls(route), route.path


def test_the_ui_can_be_switched_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spy = SpyWriteClient()
    with build_ui(tmp_path, monkeypatch, spy, ui_enabled=False) as client:
        assert client.get("/ui/incidents", headers=OPERATOR).status_code == 404
        assert client.get("/incidents", headers=OPERATOR).status_code == 200
        assert "content-security-policy" not in client.get("/health").headers


def test_dev_headers_mode_serves_the_ui_locally(tmp_path: Path) -> None:
    with TestClient(create_app(make_settings(tmp_path))) as client:
        page = client.get("/ui/incidents", headers={"X-Orqentis-User": "olivia@contoso.com"})
        assert page.status_code == 200 and "Sign out" not in page.text and "INC-" in page.text
