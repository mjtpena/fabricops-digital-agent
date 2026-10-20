"""``orqentis contracts scaffold`` against recorded Fabric / Power BI responses (no tenant, no credentials)."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from orqentis import bootstrap
from orqentis.cli import main
from orqentis.config.clock import FixedClock
from orqentis.config.settings import AuthMode, Settings
from orqentis.contracts import EstateContracts
from orqentis.contracts.catalog import CatalogSnapshot
from orqentis.contracts.scaffold import ScaffoldOptions, ScaffoldResult, scaffold_contracts
from orqentis.contracts.validate import validate_text
from orqentis.domain.common import Environment
from orqentis.fabric.errors import FabricError
from orqentis.fabric.rest.auth import POWERBI_SCOPE, InteractiveTokenProvider
from orqentis.fabric.rest.client import RestFabricClient
from orqentis.fabric.rest.http import FabricHttp
from orqentis.fabric.rest.scanner import PowerBiScanner
from tests.conftest import REPO
from tests.integration.test_fabric_rest_client import FABRIC, POWERBI, FakeTokenProvider, SleepRecorder
from tests.support.recorded_transport import RecordedTransport

TENANT = "tenant-test"
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
OPTIONS = ScaffoldOptions(tenant_id=TENANT, timezone="Australia/Sydney")
LEAKED = ("Pa55w0rd-do-not-leak", "abcdef0123456789", "svc-user")


def read_client(transport: RecordedTransport) -> RestFabricClient:
    return RestFabricClient(
        fabric_base_url=FABRIC,
        powerbi_base_url=POWERBI,
        token_provider=FakeTokenProvider(),
        contracts=EstateContracts(),  # the scaffold classifies by inference only
        tenant_id=TENANT,
        transport=httpx.MockTransport(transport),
        sleep=SleepRecorder(),
        clock=FixedClock(NOW),
    )


def scanner_for(transport: RecordedTransport, sleeper: SleepRecorder | None = None, **options: Any) -> PowerBiScanner:
    http = FabricHttp(FakeTokenProvider(), transport=httpx.MockTransport(transport), sleep=SleepRecorder())
    return PowerBiScanner(http, POWERBI, sleep=sleeper or SleepRecorder(), **options)


async def run_scaffold(
    *, scanner: bool = False, options: ScaffoldOptions = OPTIONS
) -> tuple[ScaffoldResult, RecordedTransport]:
    transport = RecordedTransport(allow_scanner=scanner)
    fabric = read_client(transport)
    lineage = scanner_for(transport) if scanner else None
    try:
        return await scaffold_contracts(fabric, options, scanner=lineage), transport
    finally:
        await fabric.aclose()
        if lineage is not None:
            await lineage.aclose()


def edges(result: ScaffoldResult) -> set[tuple[str, str, str]]:
    return {(e.source_item_id, e.target_item_id, e.dependency_type.value) for e in result.contracts.dependencies}


# ------------------------------------------------------------------------------------ the draft
async def test_scaffold_produces_a_file_that_passes_validate() -> None:
    result, transport = await run_scaffold()
    text = result.contracts_json()
    report = validate_text(text, Path("draft.json"), catalog=result.catalog, tenant_id=TENANT)
    assert report.errors == ()
    assert report.warnings  # a draft is never "clean": inferred entries and TODO placeholders need review
    reloaded = validate_text(text, Path("draft.json")).contracts
    assert reloaded == result.contracts  # what is written is what was built
    assert transport.unexpected == []


async def test_scaffold_pre_fills_inferred_environments() -> None:
    result, _ = await run_scaffold()
    declared = {e.workspace_id: e for e in result.contracts.environments}
    assert {k: v.environment for k, v in declared.items()} == {
        "ws-sales": Environment.PROD,
        "ws-fin": Environment.DEV,
        "ws-shared": Environment.UNKNOWN,  # nothing in the name: a human must decide
    }
    assert all(e.inferred for e in declared.values())
    assert "Sales Analytics [PROD]" in (declared["ws-sales"].note or "")
    assert any("no environment in their name" in note for note in result.notes)


async def test_scaffold_draws_triggers_edges_from_observed_activity_inputs() -> None:
    result, _ = await run_scaffold()
    assert edges(result) == {
        ("pl-sales", "nb-sales", "TRIGGERS"),  # {"notebookId": ...}
        ("pl-sales", "pl-fin", "TRIGGERS"),  # {"pipelineId": ...}, across workspaces
        ("pl-sales", "sm-sales", "REFRESHES"),  # {"semanticModelId": ...}
        ("pl-fin", "nb-fin", "TRIGGERS"),  # {"typeProperties": {"notebookId": ...}}: nested input
    }
    trigger = next(e for e in result.contracts.dependencies if e.target_item_id == "nb-sales")
    assert trigger.source == "observed" and trigger.metadata["activities"] == ["transform_sales"]
    assert trigger.metadata["runs_observed"] == 2  # seen in both recent runs, one edge
    # What cannot be known stays out of the draft, and the operator is told why.
    assert not any(e.target_item_id in ("lh-sales", "nb-ghost") for e in result.contracts.dependencies)
    assert any("lakehouses or warehouses" in note for note in result.notes)
    assert any("nb-ghost is not in the scanned workspaces" in note for note in result.notes)


async def test_scaffold_adds_marked_placeholders_for_sources_products_and_slas() -> None:
    result, _ = await run_scaffold()
    contracts = result.contracts
    (source,) = contracts.external_sources
    assert source.metadata["draft"] is True and source.owner == "TODO: assign an owner"
    products = {p.workspace_id: p for p in contracts.data_products}
    assert set(products) == {"ws-sales", "ws-shared"}  # only workspaces that hold a lakehouse or warehouse
    assert products["ws-sales"].backing_item_ids == ["lh-sales"]
    assert all(p.metadata["draft"] is True for p in products.values())
    (sla,) = contracts.slas  # ws-shared has no pipeline to measure freshness with
    assert sla.data_product_id == products["ws-sales"].id and sla.freshness_item_id == "pl-sales"
    assert sla.timezone == "Australia/Sydney" and sla.metadata["draft"] is True
    report = validate_text(result.contracts_json(), catalog=result.catalog)
    drafts = [w for w in report.warnings if "draft placeholder" in w.message]
    assert len(drafts) == len(contracts.external_sources) + len(products) + len(contracts.slas)
    assert not report.ok(strict=True)  # --strict is how CI insists that a human reviewed it


async def test_scaffold_is_deterministic() -> None:
    first, _ = await run_scaffold()
    second, _ = await run_scaffold()
    assert first.contracts_json() == second.contracts_json()


async def test_scaffold_makes_no_write_calls() -> None:
    result, transport = await run_scaffold()
    assert transport.rejected == [] and transport.unexpected == []
    assert transport.methods == {"GET", "POST"}
    # The one POST is the read-only activity-run query; the Scanner endpoint is never touched without --scanner.
    assert all(r.url.path.endswith("/queryactivityruns") for r in transport.requests if r.method == "POST")
    assert transport.calls("POST", "/getInfo") == []
    assert all(r.headers["Authorization"].startswith("Bearer ") for r in transport.requests)
    assert result.stats["workspaces"] == 3 and result.stats["items"] == 10


async def test_the_recorded_transport_really_refuses_writes() -> None:
    transport = RecordedTransport()
    client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    for method in ("PUT", "PATCH", "DELETE"):
        assert (await client.request(method, f"{FABRIC}/workspaces/ws-sales")).status_code == 405
    assert (await client.post(f"{FABRIC}/workspaces")).status_code == 405
    assert (
        await client.post(f"{POWERBI}/admin/workspaces/getInfo")
    ).status_code == 405  # scanner POST needs scanner mode
    assert len(transport.rejected) == 5
    await client.aclose()


# --------------------------------------------------------------------------------- scanner mode
async def test_scanner_mode_adds_lineage_and_data_sources() -> None:
    result, transport = await run_scaffold(scanner=True)
    assert edges(result) >= {
        ("sm-sales", "sm-fin", "READS_FROM"),  # upstream semantic model
        ("rp-sales", "sm-sales", "DEPENDS_ON"),  # report -> its semantic model
    }
    sources = {s.id: s for s in result.contracts.external_sources}
    assert len(sources) == 2 and all(s.metadata["draft"] is True for s in sources.values())
    sql = next(s for s in sources.values() if s.metadata["datasource_kind"] == "Sql")
    assert sql.display_name == "Sql: sql-prod.contoso.com / CRM"
    assert ("sm-sales", sql.id, "READS_FROM") in edges(result)
    # Credentials and query strings in a data source URL never reach the draft.
    text = result.contracts_json()
    for leaked in LEAKED:
        assert leaked not in text
    assert "https://api.contoso.com/v2/orders" in text
    assert validate_text(text, catalog=result.catalog, tenant_id=TENANT).errors == ()
    # The only non-GET requests are the read-only query and the Scanner's getInfo.
    assert transport.rejected == [] and transport.unexpected == []
    (get_info,) = transport.calls("POST", "/getInfo")
    assert json.loads(get_info.content)["workspaces"] == ["ws-fin", "ws-sales", "ws-shared"]
    assert get_info.url.params["lineage"] == "True" and get_info.url.params["datasourceDetails"] == "True"
    assert len(transport.calls("GET", "/scanStatus/scan-1")) == 2  # Running, then Succeeded
    assert len(transport.calls("GET", "/scanResult/scan-1")) == 1


async def test_scanner_failure_keeps_the_rest_of_the_draft() -> None:
    transport = RecordedTransport(allow_scanner=True)
    fabric = read_client(transport)

    class Forbidden:
        async def scan(self, workspace_ids: Any) -> Any:
            raise FabricError("denied", status_code=403, error_code="Unauthorized")

        async def aclose(self) -> None: ...

    try:
        result = await scaffold_contracts(fabric, OPTIONS, scanner=Forbidden())
    finally:
        await fabric.aclose()
    assert any("scanner lineage not added (Unauthorized)" in n and "Fabric administrator" in n for n in result.notes)
    assert ("pl-sales", "nb-sales", "TRIGGERS") in edges(result)


# ----------------------------------------------------------------------------- partial estates
async def test_workspace_filter_limits_what_is_read() -> None:
    options = ScaffoldOptions(tenant_id=TENANT, workspaces=("Sales Analytics [PROD]",))
    result, transport = await run_scaffold(options=options)
    assert [e.workspace_id for e in result.contracts.environments] == ["ws-sales"]
    assert edges(result) == {
        ("pl-sales", "nb-sales", "TRIGGERS"),
        ("pl-sales", "sm-sales", "REFRESHES"),
    }  # pl-fin is outside the selection, so the edge to it is left out
    assert any("pl-fin is not in the scanned workspaces" in n for n in result.notes)
    assert transport.calls("GET", "/ws-fin/items") == []

    nothing, _ = await run_scaffold(options=ScaffoldOptions(tenant_id=TENANT, workspaces=("No such workspace",)))
    assert nothing.contracts.environments == [] and any("no workspace matched" in n for n in nothing.notes)


async def test_an_unreadable_workspace_is_reported_not_fatal() -> None:
    class Router(RecordedTransport):
        def __call__(self, request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/ws-fin/items"):
                self.requests.append(request)
                return httpx.Response(403, json={"errorCode": "InsufficientPrivileges", "message": "no"})
            return super().__call__(request)

    transport = Router()
    fabric = read_client(transport)
    try:
        result = await scaffold_contracts(fabric, OPTIONS)
    finally:
        await fabric.aclose()
    assert any("workspace ws-fin: items not listed (InsufficientPrivileges)" in n for n in result.notes)
    assert "ws-fin" in {e.workspace_id for e in result.contracts.environments}  # still drafted from its name
    assert validate_text(result.contracts_json(), catalog=result.catalog).errors == ()


# ----------------------------------------------------------------------------- the scanner adapter
async def test_scanner_polls_until_the_scan_succeeds_and_batches_workspaces() -> None:
    transport = RecordedTransport(allow_scanner=True)
    sleeper = SleepRecorder()
    scanner = scanner_for(transport, sleeper)
    scan = await scanner.scan([f"ws-{i}" for i in range(250)])
    await scanner.aclose()
    assert len(transport.calls("POST", "/getInfo")) == 3  # at most 100 workspaces per request
    assert [len(json.loads(r.content)["workspaces"]) for r in transport.calls("POST", "/getInfo")] == [100, 100, 50]
    assert sleeper.delays and set(sleeper.delays) == {3.0}  # polled while the scan was running
    assert {d.id for d in scan.datasets} == {"sm-sales", "sm-fin"}
    assert {s.id for s in scan.datasources} == {"ds-sql", "ds-web"}
    assert all("@" not in s.detail and "?" not in s.detail for s in scan.datasources)


@pytest.mark.parametrize(
    ("state", "code"),
    [("Failed", "ScanFailed"), ("Running", "ScanTimeout")],
)
async def test_scanner_reports_failed_and_stuck_scans(state: str, code: str) -> None:
    class Stuck(RecordedTransport):
        def __call__(self, request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/scanStatus/scan-1"):
                self.requests.append(request)
                return httpx.Response(200, json={"id": "scan-1", "status": state})
            return super().__call__(request)

    scanner = scanner_for(Stuck(allow_scanner=True), max_wait_seconds=9.0)
    with pytest.raises(FabricError) as excinfo:
        await scanner.scan(["ws-sales"])
    assert excinfo.value.error_code == code
    await scanner.aclose()


# -------------------------------------------------------------------------------------- the CLI
def patch_clients(monkeypatch: pytest.MonkeyPatch, *, scanner: bool = False) -> RecordedTransport:
    transport = RecordedTransport(allow_scanner=scanner)

    async def build_discovery_client(settings: Settings) -> RestFabricClient:
        assert settings.tenant_id == TENANT
        return read_client(transport)

    async def build_lineage_scanner(settings: Settings, *, client_id: str | None = None) -> PowerBiScanner:
        assert client_id == "public-client-id"
        return scanner_for(transport)

    monkeypatch.setattr(bootstrap, "build_discovery_client", build_discovery_client)
    monkeypatch.setattr(bootstrap, "build_lineage_scanner", build_lineage_scanner)
    return transport


def test_cli_scaffold_then_validate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    transport = patch_clients(monkeypatch)
    draft, catalog = tmp_path / "out" / "contracts.draft.json", tmp_path / "out" / "catalog.json"
    args = ["contracts", "scaffold", "--tenant-id", TENANT, "--output", str(draft), "--catalog-output", str(catalog)]
    assert main(args) == 0
    out = capsys.readouterr().out
    assert "Wrote draft" in out and "draft for human review" in out and "still need review" in out
    assert transport.rejected == []
    # The written draft passes `contracts validate`, with the written catalog, and fails it under --strict.
    assert main(["contracts", "validate", str(draft), "--catalog", str(catalog)]) == 0
    assert main(["contracts", "validate", str(draft), "--catalog", str(catalog), "--strict"]) == 1
    assert CatalogSnapshot.load(catalog).items.keys() >= {"pl-sales", "nb-fin", "lh-shared"}
    # Never overwrites by accident.
    assert main(args) == 1
    assert "already exists" in capsys.readouterr().out
    assert main([*args, "--force"]) == 0


def test_cli_scaffold_requires_a_tenant(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["contracts", "scaffold", "--output", str(tmp_path / "draft.json")]) == 2
    assert "ORQENTIS_TENANT_ID" in capsys.readouterr().out
    assert not (tmp_path / "draft.json").exists()


def test_cli_scanner_mode_is_interactive_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from orqentis.contracts import commands

    patch_clients(monkeypatch, scanner=True)
    args = ["contracts", "scaffold", "--tenant-id", TENANT, "--output", str(tmp_path / "d.json"), "--scanner"]
    args += ["--scanner-client-id", "public-client-id"]
    monkeypatch.setattr(commands, "_interactive", lambda: False)  # a script, a pipe, a scheduler
    assert main(args) == 2
    assert "interactive" in capsys.readouterr().out and not (tmp_path / "d.json").exists()

    monkeypatch.setattr(commands, "_interactive", lambda: True)  # an administrator at a terminal
    assert main(args) == 0
    draft = json.loads((tmp_path / "d.json").read_text(encoding="utf-8"))
    assert any(d["dependency_type"] == "DEPENDS_ON" for d in draft["dependencies"])
    assert all(leaked not in json.dumps(draft) for leaked in LEAKED)


# ---------------------------------------------------- the scanner never reaches the application runtime
def test_the_application_never_imports_the_scanner() -> None:
    code = (
        "import sys\n"
        "import orqentis.api.app, orqentis.bootstrap\n"
        "tools = ('orqentis.fabric.rest.scanner', 'orqentis.contracts.scaffold', 'orqentis.contracts.commands')\n"
        "bad = [m for m in tools if m in sys.modules]\n"
        "assert not bad, bad\n"
    )
    command = [sys.executable, "-I", "-c", code]
    done = subprocess.run(command, capture_output=True, text=True, cwd=REPO, check=False)  # noqa: S603
    assert done.returncode == 0, done.stderr


def test_interactive_sign_in_is_not_an_auth_mode() -> None:
    assert {m.value for m in AuthMode} == {"default", "managed_identity", "workload_identity", "service_principal"}


async def test_interactive_token_provider_uses_the_configured_cloud(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[dict[str, Any]] = []
    requested: list[str] = []

    class FakeBrowserCredential:
        def __init__(self, **kwargs: Any) -> None:
            created.append(kwargs)

        def get_token(self, scope: str) -> Any:
            requested.append(scope)
            return SimpleNamespace(token="admin-token", expires_on=0)

        def close(self) -> None: ...

    monkeypatch.setattr("orqentis.fabric.rest.auth.identity.InteractiveBrowserCredential", FakeBrowserCredential)
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        authority_host="https://login.microsoftonline.us",
        powerbi_scope="https://analysis.usgovcloudapi.net/powerbi/api/.default",
        tenant_id="tenant-gov",
    )
    provider = InteractiveTokenProvider(settings, client_id="my-public-client")
    assert await provider.get_token(POWERBI_SCOPE) == "admin-token"
    await provider.close()
    assert created == [
        {"authority": "https://login.microsoftonline.us", "client_id": "my-public-client", "tenant_id": "tenant-gov"}
    ]
    assert requested == ["https://analysis.usgovcloudapi.net/powerbi/api/.default"]
