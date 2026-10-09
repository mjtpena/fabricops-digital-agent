"""REST API (FastAPI TestClient against a container built on the synthetic estate)."""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from orqentis.api.app import create_app
from orqentis.config.settings import ApiAuthMode
from tests.conftest import make_settings

OPERATOR = {"X-Orqentis-User": "olivia@contoso.com", "X-Orqentis-Roles": "FabricOps.Operator"}
VIEWER = {"X-Orqentis-User": "victor@contoso.com"}


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    with TestClient(create_app(make_settings(tmp_path))) as test_client:
        yield test_client


def investigated(client: TestClient) -> dict[str, object]:
    incident_id = client.get("/incidents", headers=VIEWER).json()[0]["incident_id"]
    response = client.post(f"/incidents/{incident_id}/investigate", headers=VIEWER)
    assert response.status_code == 200, response.text
    body: dict[str, object] = response.json()
    return body


def test_health_and_openapi(client: TestClient) -> None:
    health = client.get("/health").json()
    assert health["status"] == "ok" and health["fabric_mode"] == "mock" and health["write_actions_enabled"] is False
    paths = client.get("/openapi.json").json()["paths"]
    for path in (
        "/health",
        "/incidents",
        "/incidents/{incident_id}",
        "/incidents/{incident_id}/investigate",
        "/incidents/{incident_id}/evidence",
        "/incidents/{incident_id}/recommendations",
        "/runbooks",
        "/estate/workspaces",
        "/estate/items/{item_id}/dependencies",
        "/approvals/{approval_id}/approve",
        "/approvals/{approval_id}/reject",
    ):
        assert path in paths


def test_incident_lifecycle_over_http(client: TestClient) -> None:
    listing = client.get("/incidents", headers=VIEWER).json()
    assert (listing[0]["incident_id"], listing[0]["status"]) == ("INC-2026-0042", "DETECTED")
    assert {i["status"] for i in listing[1:]} == {"CLOSED"}  # synthetic incident history
    result = investigated(client)
    assert result["incident"]["status"] == "AWAITING_APPROVAL"  # type: ignore[index]
    assert result["incident"]["category"] == "SCHEMA_DRIFT"  # type: ignore[index]
    incident = client.get("/incidents/INC-2026-0042", headers=VIEWER).json()
    assert incident["probable_root_causes"][0]["category"] == "SCHEMA_DRIFT"
    evidence = client.get("/incidents/INC-2026-0042/evidence", headers=VIEWER).json()
    assert {e["evidence_type"] for e in evidence} >= {"ERROR_MESSAGE", "SCHEMA_CHANGE", "BLAST_RADIUS"}
    recommendations = client.get("/incidents/INC-2026-0042/recommendations", headers=VIEWER).json()
    assert recommendations[0]["runbook_id"] == "RB-SCHEMA-DRIFT-001"
    markdown = client.get("/incidents/INC-2026-0042/report", params={"format": "markdown"}, headers=VIEWER)
    assert markdown.headers["content-type"].startswith("text/markdown")
    assert "## Probable Root Cause" in markdown.text
    filtered = client.get("/incidents", params={"status": "AWAITING_APPROVAL"}, headers=VIEWER).json()
    assert [i["incident_id"] for i in filtered] == ["INC-2026-0042"]


def test_approval_flow_and_errors(client: TestClient) -> None:
    approvals = investigated(client)["approval_ids"]
    assert isinstance(approvals, list)
    forbidden = client.post(f"/approvals/{approvals[0]}/approve", headers=VIEWER)
    assert forbidden.status_code == 403 and forbidden.json()["error"] == "APPROVAL_FORBIDDEN"
    approved = client.post(f"/approvals/{approvals[0]}/approve", headers=OPERATOR, json={"comment": "LGTM"})
    assert approved.status_code == 200 and approved.json()["status"] == "APPROVED"
    again = client.post(f"/approvals/{approvals[0]}/reject", headers=OPERATOR)
    assert again.status_code == 409
    blocked = client.post(f"/approvals/{approvals[0]}/execute", headers=OPERATOR)
    assert blocked.status_code == 403 and blocked.json()["error"] == "WRITE_CAPABILITY_NOT_GRANTED"
    rejected = client.post(f"/approvals/{approvals[1]}/reject", headers=OPERATOR)
    assert rejected.json()["status"] == "REJECTED"
    assert client.post("/approvals/APR-missing/approve", headers=OPERATOR).status_code == 404
    pending = client.get("/approvals", params={"status": "PENDING"}, headers=VIEWER).json()
    assert pending == []


def test_reserved_principals_cannot_be_impersonated(client: TestClient) -> None:
    approvals = investigated(client)["approval_ids"]
    assert isinstance(approvals, list)
    response = client.post(
        f"/approvals/{approvals[0]}/approve",
        headers={"X-Orqentis-User": "agent:FabricOpsInvestigator", "X-Orqentis-Roles": "FabricOps.Operator"},
    )
    assert response.status_code == 403


def test_estate_and_runbooks(client: TestClient) -> None:
    workspaces = client.get("/estate/workspaces", headers=VIEWER).json()
    assert len(workspaces) == 7
    deps = client.get("/estate/items/nb-transform-sales/dependencies", headers=VIEWER).json()
    assert {n["item_id"] for n in deps["upstream"]} >= {"lh-sales-bronze", "src-crm-sales-orders"}
    assert deps["blast_radius"]["cross_workspace"] is True
    assert client.get("/estate/items/nope/dependencies", headers=VIEWER).status_code == 404
    runbooks = client.get("/runbooks", headers=VIEWER).json()
    assert len(runbooks) == 7
    assert client.get("/runbooks/RB-SCHEMA-DRIFT-001", headers=VIEWER).json()["category"] == "SCHEMA_DRIFT"


def test_unknown_incident_and_conflicts(client: TestClient) -> None:
    assert client.get("/incidents/INC-0000-0000", headers=VIEWER).status_code == 404
    investigated(client)
    collect = client.post("/telemetry/collect", headers=VIEWER).json()
    assert collect["incidents_created"] == []
    assert client.get("/audit/verify", headers=VIEWER).json()["valid"] is True
    assert client.get("/audit", params={"incident_id": "INC-2026-0042"}, headers=VIEWER).json()


def test_easy_auth_principal(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, api_auth_mode=ApiAuthMode.EASY_AUTH)
    principal = {
        "auth_typ": "aad",
        "name_typ": "name",
        "role_typ": "roles",
        "claims": [
            {"typ": "http://schemas.microsoft.com/identity/claims/objectidentifier", "val": "oid-123"},
            {"typ": "name", "val": "Olivia"},
            {"typ": "roles", "val": "FabricOps.Operator"},
        ],
    }
    header = {"X-MS-CLIENT-PRINCIPAL": base64.b64encode(json.dumps(principal).encode()).decode()}
    with TestClient(create_app(settings)) as client:
        assert client.get("/incidents").status_code == 401
        assert client.get("/incidents", headers={"X-MS-CLIENT-PRINCIPAL": "%%%"}).status_code == 401
        incident_id = client.get("/incidents", headers=header).json()[0]["incident_id"]
        approvals = client.post(f"/incidents/{incident_id}/investigate", headers=header).json()["approval_ids"]
        # Requester is the agent, approver is the Easy Auth principal with the Operator role.
        approved = client.post(f"/approvals/{approvals[0]}/approve", headers=header)
        assert approved.status_code == 200 and approved.json()["approved_by"] == "oid-123"


def test_dev_headers_are_refused_outside_local() -> None:
    with pytest.raises(ValueError, match="dev_headers"):
        make_settings(Path("/tmp"), deployment_environment="prod")
