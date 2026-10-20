"""A REST-mode application on a mocked Fabric transport, with the contracts file under the test's control.

Used by the contracts reload tests (file and blob): the Fabric/Power BI HTTP layer is mocked, everything above it is the
production code path, and the contracts come from whichever source the settings name.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from orqentis.config.clock import FixedClock
from orqentis.config.settings import Settings
from orqentis.contracts import EstateContracts
from orqentis.fabric.rest.client import RestFabricClient
from tests.integration.test_fabric_rest_client import (
    FABRIC,
    POWERBI,
    FakeTokenProvider,
    Router,
    SleepRecorder,
)
from tests.integration.test_rest_mode_composition import estate_routes

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


def dependency(identifier: str, source: str, target: str, kind: str) -> dict[str, str]:
    return {
        "id": identifier,
        "tenant_id": "tenant-test",
        "source_item_id": source,
        "target_item_id": target,
        "dependency_type": kind,
    }


BASE_DEPENDENCIES = [
    dependency("d1", "pl-1", "nb-1", "TRIGGERS"),
    dependency("d2", "nb-1", "lh-1", "WRITES_TO"),
    dependency("d3", "sm-1", "lh-1", "READS_FROM"),
]
EXTRA_DEPENDENCY = dependency("d4", "sm-1", "nb-1", "READS_FROM")


def contracts_bytes(*extra: dict[str, str], layout: str = "compact") -> bytes:
    """A contracts document; ``layout`` only changes the bytes (hence the SHA-256), not the meaning."""
    document: dict[str, Any] = {"dependencies": [*BASE_DEPENDENCIES, *extra]}
    if layout == "compact":
        return json.dumps(document).encode()
    return json.dumps(document, indent=2).encode()


def mock_fabric(monkeypatch: pytest.MonkeyPatch) -> Router:
    """Route the REST client's HTTP to a mocked estate; returns the router so a test can change the estate."""
    router = Router()
    estate_routes(router)
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
    return router
