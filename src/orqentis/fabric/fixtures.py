"""Synthetic estate and scenario telemetry, validated against the domain model."""

from __future__ import annotations

from pathlib import Path

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from orqentis.contracts.model import EstateContracts
from orqentis.domain.deployment import DeploymentState
from orqentis.domain.estate import Capacity, FabricItem, Tenant, Workspace
from orqentis.domain.events import OperationalEvent
from orqentis.domain.incidents import Incident
from orqentis.domain.runs import ActivityRun, CapacitySnapshot, PipelineRun, Refresh


class _Fixture(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EstateFixture(EstateContracts):
    """A complete synthetic Fabric estate."""

    tenant: Tenant
    capacities: list[Capacity]
    workspaces: list[Workspace]
    items: list[FabricItem]
    deployment_states: list[DeploymentState] = Field(default_factory=list)


class ScenarioInfo(_Fixture):
    name: str
    title: str
    description: str
    now: AwareDatetime
    expected_category: str
    expected_runbook_id: str | None = None


class ScenarioFixture(_Fixture):
    scenario: ScenarioInfo
    pipeline_runs: list[PipelineRun] = Field(default_factory=list)
    activity_runs: list[ActivityRun] = Field(default_factory=list)
    refreshes: list[Refresh] = Field(default_factory=list)
    capacity_snapshots: list[CapacitySnapshot] = Field(default_factory=list)
    # Events emitted by collectors that have no Fabric REST equivalent (e.g. a Delta-log schema watcher).
    events: list[OperationalEvent] = Field(default_factory=list)


class HistoryFixture(_Fixture):
    incidents: list[Incident]


def _read(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(f"Fixture not found: {path}")
    return path.read_text(encoding="utf-8")


def load_estate(fixtures_dir: Path) -> EstateFixture:
    return EstateFixture.model_validate_json(_read(fixtures_dir / "fabric" / "estate.json"))


def load_scenario(fixtures_dir: Path, name: str) -> ScenarioFixture:
    return ScenarioFixture.model_validate_json(_read(fixtures_dir / "telemetry" / f"{name}.json"))


def list_scenarios(fixtures_dir: Path) -> list[ScenarioInfo]:
    out: list[ScenarioInfo] = []
    for path in sorted((fixtures_dir / "telemetry").glob("*.json")):
        out.append(ScenarioFixture.model_validate_json(path.read_text(encoding="utf-8")).scenario)
    return out


def load_history(fixtures_dir: Path) -> list[Incident]:
    return HistoryFixture.model_validate_json(_read(fixtures_dir / "incidents" / "history.json")).incidents
