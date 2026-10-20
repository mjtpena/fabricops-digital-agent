"""Live tests: the read-only REST adapter against the FabricOps sandbox, as the least-privilege test identity.

These tests are skipped unless ``ORQENTIS_LIVE_TESTS=1``. They need:

* the sandbox estate (``scripts/live-estate/create.sh``) with the capacity running
  (``scripts/live-estate/capacity.sh resume``);
* the test service principal's credentials in the environment as ``AZURE_TENANT_ID``,
  ``AZURE_CLIENT_ID`` and ``AZURE_CLIENT_SECRET`` (``ORQENTIS_AUTH_MODE=service_principal`` reads them
  through ``EnvironmentCredential``), or in the env file named by ``FABRICOPS_LIVE_ENV_FILE``
  (default ``~/.fabricops/live-reader.env``). The secret is never printed, logged or written anywhere.

The principal holds the workspace **Viewer** role on the four sandbox workspaces and nothing else, so
every read below is also a proof of what Viewer alone is enough for.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from orqentis.config.settings import Settings
from orqentis.domain.common import Environment
from orqentis.domain.estate import Workspace
from orqentis.fabric.fixtures import EstateContracts
from orqentis.fabric.rest.auth import AzureIdentityTokenProvider, build_async_credential
from orqentis.fabric.rest.client import RestFabricClient

LIVE_ENABLED = os.environ.get("ORQENTIS_LIVE_TESTS") == "1"  # read at import: the autouse fixture clears ORQENTIS_*

SANDBOX_WORKSPACES = {
    "FabricOps Sandbox [DEV]": Environment.DEV,
    "FabricOps Sandbox [TEST]": Environment.TEST,
    "FabricOps Sandbox [PROD]": Environment.PROD,
    "FabricOps Sandbox Consumer": Environment.UNKNOWN,
}
PIPELINE = "ingest_customers"
NOTEBOOK = "transform_customers"
LAKEHOUSE = "crm_source"
MODEL = "customers_model"
REPORT = "customers_report"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    here = Path(__file__).parent
    skip = pytest.mark.skip(reason="live tests need ORQENTIS_LIVE_TESTS=1 (see tests/live/conftest.py)")
    for item in items:
        if here in Path(str(item.fspath)).parents:
            item.add_marker(pytest.mark.live)
            if not LIVE_ENABLED:
                item.add_marker(skip)


def _load_env_file() -> None:
    """Load AZURE_* from the owner-provided env file when they are not already set (never printed)."""
    if os.environ.get("AZURE_CLIENT_SECRET"):
        return
    path = Path(os.environ.get("FABRICOPS_LIVE_ENV_FILE", Path.home() / ".fabricops" / "live-reader.env"))
    if not path.is_file():
        pytest.fail(f"no AZURE_CLIENT_SECRET in the environment and no env file at {path}")
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip().removeprefix("export ")
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            if key.strip().startswith("AZURE_"):
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class Sandbox:
    """Ids of the sandbox estate, discovered through the adapter itself (names are the contract)."""

    workspaces: dict[str, Workspace]

    def workspace(self, suffix: str) -> Workspace:
        """``"[DEV]"``, ``"[TEST]"``, ``"[PROD]"`` or ``"Consumer"``."""
        return self.workspaces[f"FabricOps Sandbox {suffix}"]


@pytest.fixture(scope="session", autouse=True)
def _live_credentials() -> None:
    if LIVE_ENABLED:
        _load_env_file()


@pytest.fixture
async def rest_client() -> AsyncIterator[RestFabricClient]:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        fabric_mode="rest",  # type: ignore[arg-type]
        auth_mode="service_principal",  # type: ignore[arg-type]
        tenant_id=os.environ["AZURE_TENANT_ID"],
    )
    client = RestFabricClient.from_settings(
        settings, EstateContracts(), AzureIdentityTokenProvider(build_async_credential(settings))
    )
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture
async def sandbox(rest_client: RestFabricClient) -> Sandbox:
    workspaces = {w.display_name: w for w in await rest_client.workspaces.list_workspaces()}
    return Sandbox(workspaces)
