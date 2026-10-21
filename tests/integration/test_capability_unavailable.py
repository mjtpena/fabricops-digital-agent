"""Known permission gaps are not collector errors (ledger R13): a documented 403 is "capability unavailable".

The semantic model refresh history needs dataset Write, which the reader never gets, so against a real tenant that
collector gets a 403 on every run. Reported as an error it keeps the collector-errors alert fired for ever; reported as
"unavailable" it is visible (metric, readiness, API) and quiet. An *unexpected* 401/403 stays an error.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import httpx
import pytest

from orqentis.api.app import create_app
from orqentis.bootstrap import Container, build_container
from orqentis.config.settings import Settings
from orqentis.domain.events import OperationalEvent
from orqentis.incidents.detection_runner import DetectionRunner
from orqentis.fabric.errors import (
    FabricAuthError,
    FabricCapabilityUnavailableError,
    FabricError,
    FabricNotFoundError,
)
from orqentis.runtime.collection import CollectionService
from orqentis.telemetry.collectors import TelemetryIngestor
from tests.conftest import make_settings
from tests.support.metrics import MetricsHarness


class Failing:
    """A whole-run collector that raises ``error`` (or returns nothing)."""

    def __init__(self, name: str, error: Exception | None = None) -> None:
        self.name = name
        self.error = error

    async def collect(self, *, since: datetime, until: datetime) -> list[OperationalEvent]:
        if self.error is not None:
            raise self.error
        return []


class FailingScopes:
    """A scoped collector: scope ``a`` fails with ``error``, scope ``b`` works."""

    def __init__(self, name: str, error: Exception) -> None:
        self.name = name
        self.error = error

    def scopes(self) -> list[str]:
        return ["a", "b"]

    async def collect_scope(self, scope: str, *, since: datetime, until: datetime) -> list[OperationalEvent]:
        if scope == "a":
            raise self.error
        return []


def forbidden() -> FabricAuthError:
    return FabricAuthError("GET /refreshes failed with HTTP 403", status_code=403, error_code="Unauthorized")


def ingestor(
    container: Container, *collectors: object, expected: tuple[str, ...] = (), harness: MetricsHarness | None = None
) -> TelemetryIngestor:
    return TelemetryIngestor(
        list(collectors),  # type: ignore[arg-type]
        container.event_store,
        container.clock,
        expected_forbidden=expected,
        metrics=harness.metrics if harness else container.metrics,
    )


async def test_an_expected_403_is_unavailable_and_not_an_error(container: Container) -> None:
    harness = MetricsHarness()
    result = await ingestor(
        container,
        Failing("semantic-model-refreshes", forbidden()),
        expected=("semantic-model-refreshes",),
        harness=harness,
    ).run()
    assert result.errors == {} and result.units_failed == 0
    assert "HTTP 403" in result.unavailable["semantic-model-refreshes"] and result.units_unavailable == 1
    assert harness.total("orqentis.collector.unavailable", collector="semantic-model-refreshes") == 1
    assert harness.total("orqentis.collector.errors") == 0
    assert (
        await container.event_store.get_watermarks("semantic-model-refreshes") == {}
    )  # retried from the lookback later


async def test_the_same_403_is_an_error_when_the_deployment_does_not_expect_it(container: Container) -> None:
    result = await ingestor(container, Failing("semantic-model-refreshes", forbidden())).run()
    assert "semantic-model-refreshes" in result.errors and result.unavailable == {}
    assert result.units_failed == 1
    other = await ingestor(
        container, Failing("pipeline-runs", forbidden()), expected=("semantic-model-refreshes",)
    ).run()
    assert "pipeline-runs" in other.errors  # expecting it for one collector says nothing about another


@pytest.mark.parametrize(
    "error",
    [
        FabricAuthError("expired", status_code=401, error_code="InvalidAuthenticationToken"),
        FabricNotFoundError("gone", status_code=404, error_code="ItemNotFound"),
        FabricError("boom", status_code=500),
    ],
)
async def test_only_a_403_can_be_an_expected_gap(container: Container, error: FabricError) -> None:
    result = await ingestor(
        container, Failing("semantic-model-refreshes", error), expected=("semantic-model-refreshes",)
    ).run()
    assert "semantic-model-refreshes" in result.errors and result.unavailable == {}


async def test_a_capability_the_platform_does_not_expose_is_always_unavailable(container: Container) -> None:
    error = FabricCapabilityUnavailableError("no public API", error_code="CapacityMetricsUnavailable")
    result = await ingestor(container, Failing("capacity-metrics", error)).run()
    assert result.errors == {} and "capacity-metrics" in result.unavailable


async def test_one_scope_can_be_unavailable_while_the_others_work(container: Container) -> None:
    collector = FailingScopes("workspace-monitoring-refreshes", FabricCapabilityUnavailableError("not enabled"))
    result = await ingestor(container, collector).run()
    assert result.units_ok == 1 and result.units_unavailable == 1 and result.units_failed == 0
    assert list(result.unavailable) == ["workspace-monitoring-refreshes:a"]
    marks = await container.event_store.get_watermarks("workspace-monitoring-refreshes")
    assert set(marks) == {"b"}  # the unavailable scope keeps no watermark


async def test_a_run_whose_only_trouble_is_a_known_gap_is_a_success(container: Container) -> None:
    harness = MetricsHarness()
    service = CollectionService(
        ingestor(container, Failing("capacity-metrics", FabricCapabilityUnavailableError("no API")), harness=harness),
        DetectionRunner(container.event_store, container.detection),
        metrics=harness.metrics,
        clock=container.clock,
    )
    assert service.unavailable == []
    await service.run()
    assert service.unavailable == ["capacity-metrics"]
    assert harness.total("orqentis.collection.runs", outcome="success") == 1
    assert harness.gauge("orqentis.collection.heartbeat") is not None  # FabricOps is not blind: the heartbeat advances


async def test_readiness_names_the_unavailable_collectors_and_nothing_else(tmp_path: Path) -> None:
    settings: Settings = make_settings(tmp_path)
    container = await build_container(settings, reset=True)
    try:
        # a scope label ("collector:scope") reduces to the collector name: scopes and messages never reach the probe
        container.collection._unavailable = {"capacity-metrics:cap-secret-id": "forbidden: client_secret=hunter2"}
        app = create_app(settings, container=container)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http,  # type: ignore[arg-type]
        ):
            ready = await http.get("/health/ready")
        assert ready.status_code == 200
        assert ready.json()["unavailable"] == ["capacity-metrics"]
        assert "cap-secret-id" not in ready.text and "hunter2" not in ready.text
    finally:
        await container.aclose()
