"""Every declared environment that changes a classification is logged and audited, tied to the contracts file."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from orqentis.bootstrap import build_container
from orqentis.config.clock import FixedClock
from orqentis.config.settings import FabricMode, Settings
from orqentis.contracts import EstateContracts
from orqentis.fabric.rest.client import RestFabricClient
from orqentis.observability.audit import AuditRecord
from orqentis.topology.environment_audit import CONTRACTS_LOADED_ACTION, OVERRIDE_ACTION
from tests.conftest import REPO
from tests.integration.test_fabric_rest_client import (
    FABRIC,
    FABRIC_PATH,
    POWERBI,
    FakeTokenProvider,
    Router,
    SleepRecorder,
    respond,
)
from tests.integration.test_rest_mode_composition import NOW, estate_routes


async def started(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    document: dict[str, Any],
    extra_routes: Callable[[Router], None] | None = None,
) -> tuple[Any, str]:
    router = Router()
    estate_routes(router)  # one workspace, "Sales [PROD]" (ws-1), with a pipeline, notebook, lakehouse, model
    if extra_routes is not None:
        extra_routes(router)
    tokens = FakeTokenProvider()

    def from_settings(
        cls: type[RestFabricClient], settings: Settings, contracts: EstateContracts, token_provider: object
    ) -> RestFabricClient:
        return RestFabricClient(
            fabric_base_url=FABRIC,
            powerbi_base_url=POWERBI,
            token_provider=tokens,
            contracts=contracts,
            tenant_id=settings.tenant_id,
            transport=httpx.MockTransport(router),
            sleep=SleepRecorder(),
            clock=FixedClock(NOW),
        )

    monkeypatch.setattr(RestFabricClient, "from_settings", classmethod(from_settings))
    path = tmp_path / "contracts.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        fabric_mode=FabricMode.REST,
        tenant_id="tenant-test",
        contracts_path=path,
        data_dir=tmp_path / "var",
        runbooks_dir=REPO / "runbooks",
        fixtures_dir=REPO / "fixtures",
    )
    container = await build_container(settings, reset=True, clock=FixedClock(NOW))
    return container, hashlib.sha256(path.read_bytes()).hexdigest()


async def audit_records(container: Any, action: str) -> list[AuditRecord]:
    return [r for r in await container.audit.list() if r.action == action]


@pytest.fixture
def orqentis_logs(caplog: pytest.LogCaptureFixture) -> Any:
    logger = logging.getLogger("orqentis")
    attach = not logger.propagate
    if attach:
        logger.addHandler(caplog.handler)
    caplog.set_level(logging.DEBUG, logger="orqentis")
    yield caplog
    if attach:
        logger.removeHandler(caplog.handler)


@pytest.mark.security
async def test_a_downgrade_is_logged_and_audited_with_the_file_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, orqentis_logs: pytest.LogCaptureFixture
) -> None:
    # A single catch-all entry lowers "Sales [PROD]" to DEV: allowed, but never silent.
    document = {"environments": [{"workspace_name_pattern": "*", "environment": "DEV"}]}
    container, digest = await started(tmp_path, monkeypatch, document)
    try:
        overrides = await audit_records(container, OVERRIDE_ACTION)
        assert [r.details["id"] for r in overrides] == ["ws-1"]
        record = overrides[0]
        assert record.actor == "system:orqentis" and record.outcome == "applied"
        assert (record.details["kind"], record.details["inferred"], record.details["resolved"]) == (
            "workspace",
            "PROD",
            "DEV",
        )
        assert record.details["source"] == "declared_pattern" and record.details["downgrade"] is True
        assert record.details["contracts_sha256"] == digest
        assert (await container.audit.verify()).valid
        (loaded,) = await audit_records(container, CONTRACTS_LOADED_ACTION)
        assert loaded.details["contracts_sha256"] == digest
        assert loaded.details["environment_overrides"] == 1 and loaded.details["environment_downgrades"] == 1
        # The classification really changed, and the log says so at WARNING.
        assert container.catalog.workspaces["ws-1"].environment.value == "DEV"
        logged = [r for r in orqentis_logs.records if r.getMessage() == "environment override applied"]
        assert len(logged) == 1 and logged[0].levelno == logging.WARNING
        assert logged[0].__dict__["environment_id"] == "ws-1"
        assert logged[0].__dict__["environment_contracts_sha256"] == digest
    finally:
        await container.aclose()


async def test_declaring_what_the_name_already_says_is_not_an_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, orqentis_logs: pytest.LogCaptureFixture
) -> None:
    # Declaring what the name already says changes nothing, so nothing is recorded as an override.
    same = {"environments": [{"workspace_id": "ws-1", "environment": "PROD"}]}
    container, digest = await started(tmp_path, monkeypatch, same)
    try:
        assert await audit_records(container, OVERRIDE_ACTION) == []
        (loaded,) = await audit_records(container, CONTRACTS_LOADED_ACTION)
        assert loaded.details["contracts_sha256"] == digest and loaded.details["environment_overrides"] == 0
    finally:
        await container.aclose()


async def test_unreviewed_entries_are_counted_but_change_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    draft = {"environments": [{"workspace_id": "ws-1", "environment": "DEV", "inferred": True}]}
    container, _ = await started(tmp_path, monkeypatch, draft)
    try:
        assert await audit_records(container, OVERRIDE_ACTION) == []
        (loaded,) = await audit_records(container, CONTRACTS_LOADED_ACTION)
        assert loaded.details["unreviewed_environment_entries_ignored"] == 1
        assert container.catalog.workspaces["ws-1"].environment.value == "PROD"  # the name wins
    finally:
        await container.aclose()


async def test_mock_mode_writes_no_contracts_records(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        data_dir=tmp_path / "var",
        runbooks_dir=REPO / "runbooks",
        fixtures_dir=REPO / "fixtures",
    )
    container = await build_container(settings, reset=True)
    try:
        assert await container.audit.list() == []  # the demo's audit chain is exactly what it was
    finally:
        await container.aclose()


def late_workspace(router: Router) -> None:
    body = {"id": "ws-2", "displayName": "Sales Late [PROD]", "capacityId": "cap-1", "type": "Workspace"}
    router.add("GET", f"{FABRIC_PATH}/workspaces/ws-2", respond(body=body))
    router.add("GET", f"{FABRIC_PATH}/workspaces/ws-2/items", respond(body={"value": []}))


async def test_a_workspace_discovered_after_startup_is_audited_on_first_resolution_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    document = {"environments": [{"workspace_name_pattern": "Sales*", "environment": "DEV", "accepts_downgrade": True}]}
    container, digest = await started(tmp_path, monkeypatch, document, late_workspace)
    try:
        assert [r.details["id"] for r in await audit_records(container, OVERRIDE_ACTION)] == ["ws-1"]
        late = await container.fabric.workspaces.get_workspace("ws-2")  # discovered after the startup listing
        assert late.environment.value == "DEV" and late.metadata["environment_source"] == "declared_pattern"
        await container.fabric.workspaces.get_workspace("ws-2")  # seen again: not audited again
        await container.fabric.items.list_items("ws-2")
        overrides = await audit_records(container, OVERRIDE_ACTION)
        assert [r.details["id"] for r in overrides] == ["ws-1", "ws-2"]
        assert overrides[1].details["contracts_sha256"] == digest and overrides[1].details["downgrade"] is True
        assert (await container.audit.verify()).valid
    finally:
        await container.aclose()


async def test_item_and_capacity_overrides_are_audited_too(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    document = {
        "environments": [
            {"item_id": "nb-1", "environment": "DEV", "accepts_downgrade": True},
            {"capacity_id": "cap-1", "environment": "DEV", "accepts_downgrade": True},
        ]
    }
    container, _ = await started(tmp_path, monkeypatch, document)
    try:
        records = {(r.details["kind"], r.details["id"]): r for r in await audit_records(container, OVERRIDE_ACTION)}
        assert set(records) == {("item", "nb-1"), ("capacity", "cap-1")}
        assert (records[("item", "nb-1")].details["inferred"], records[("item", "nb-1")].details["resolved"]) == (
            "PROD",
            "DEV",
        )  # an item's baseline is its workspace's environment
        assert records[("capacity", "cap-1")].details["source"] == "declared_capacity"
        assert container.catalog.capacities["cap-1"].environment.value == "DEV"
    finally:
        await container.aclose()
