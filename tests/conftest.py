"""Shared fixtures. Tests never touch the network or a real Fabric tenant."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from orqentis.bootstrap import Container, build_container
from orqentis.config.settings import Settings
from orqentis.domain.incidents import Incident
from orqentis.domain.principals import Principal, PrincipalKind

REPO = Path(__file__).resolve().parents[1]

OPERATOR = Principal(
    id="olivia.operator@contoso.com",
    display_name="Olivia",
    kind=PrincipalKind.HUMAN,
    roles=frozenset({"FabricOps.Operator"}),
)
ADMIN = Principal(
    id="adam.admin@contoso.com",
    display_name="Adam",
    kind=PrincipalKind.HUMAN,
    roles=frozenset({"FabricOps.Operator", "FabricOps.PlatformAdmin"}),
)
VIEWER = Principal(id="victor.viewer@contoso.com", display_name="Victor", kind=PrincipalKind.HUMAN)


@pytest.fixture(autouse=True)
def _isolate_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith("ORQENTIS_") or key == "APPLICATIONINSIGHTS_CONNECTION_STRING":
            monkeypatch.delenv(key, raising=False)


def make_settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "data_dir": tmp_path / "var",
        "fixtures_dir": REPO / "fixtures",
        "runbooks_dir": REPO / "runbooks",
        "scenario": "schema_drift",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
async def container(settings: Settings) -> AsyncIterator[Container]:
    c = await build_container(settings, reset=True)
    try:
        yield c
    finally:
        await c.aclose()


async def detect(c: Container) -> Incident:
    _, created = await c.collect_and_detect()
    assert len(created) == 1, f"expected exactly one incident, got {[i.incident_id for i in created]}"
    return created[0]


async def detect_and_investigate(c: Container) -> Incident:
    incident = await detect(c)
    investigated, _ = await c.investigations.investigate(incident.incident_id, actor=OPERATOR)
    return investigated
