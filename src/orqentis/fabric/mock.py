"""In-memory Fabric adapter backed by the synthetic estate. Used for local development and tests."""

from __future__ import annotations

import json
from datetime import datetime

from orqentis.domain.common import ItemType
from orqentis.domain.delta import DeltaCommitFile, DeltaCommitRef, DeltaTableRef
from orqentis.domain.deployment import DeploymentState
from orqentis.domain.estate import Capacity, FabricItem, Workspace
from orqentis.domain.runs import ActivityRun, CapacitySnapshot, PipelineRun, Refresh
from orqentis.domain.topology import Dependency
from orqentis.fabric.errors import FabricNotFoundError
from orqentis.fabric.fixtures import DeltaLogFixture, EstateFixture, ScenarioFixture


class _MockWorkspaces:
    def __init__(self, estate: EstateFixture) -> None:
        self._by_id = {w.id: w for w in estate.workspaces}

    async def list_workspaces(self) -> list[Workspace]:
        return list(self._by_id.values())

    async def get_workspace(self, workspace_id: str) -> Workspace:
        try:
            return self._by_id[workspace_id]
        except KeyError:
            raise FabricNotFoundError(
                f"Workspace {workspace_id} not found", status_code=404, error_code="WorkspaceNotFound"
            ) from None


class _MockItems:
    def __init__(self, estate: EstateFixture) -> None:
        self._items = estate.items

    async def list_items(self, workspace_id: str, *, item_type: ItemType | None = None) -> list[FabricItem]:
        return [
            i for i in self._items if i.workspace_id == workspace_id and (item_type is None or i.item_type == item_type)
        ]

    async def get_item(self, workspace_id: str, item_id: str) -> FabricItem:
        for item in self._items:
            if item.id == item_id and item.workspace_id == workspace_id:
                return item
        raise FabricNotFoundError(
            f"Item {item_id} not found in workspace {workspace_id}", status_code=404, error_code="ItemNotFound"
        )


class _MockJobs:
    def __init__(self, scenario: ScenarioFixture) -> None:
        self._runs = scenario.pipeline_runs

    async def list_job_instances(
        self, workspace_id: str, item_id: str, *, limit: int = 50, since: datetime | None = None
    ) -> list[PipelineRun]:
        runs = [r for r in self._runs if r.workspace_id == workspace_id and r.item_id == item_id]
        if since is not None:
            runs = [r for r in runs if r.observed_at is None or r.observed_at >= since]
        runs.sort(key=lambda r: r.start_time.timestamp() if r.start_time else 0.0, reverse=True)
        return runs[:limit]

    async def get_job_instance(self, workspace_id: str, item_id: str, job_instance_id: str) -> PipelineRun:
        for run in self._runs:
            if run.id == job_instance_id and run.item_id == item_id and run.workspace_id == workspace_id:
                return run
        raise FabricNotFoundError(
            f"Job instance {job_instance_id} not found", status_code=404, error_code="JobInstanceNotFound"
        )


class _MockPipelines:
    def __init__(self, scenario: ScenarioFixture) -> None:
        self._activity_runs = scenario.activity_runs

    async def query_activity_runs(self, workspace_id: str, pipeline_id: str, job_instance_id: str) -> list[ActivityRun]:
        return [
            a
            for a in self._activity_runs
            if a.workspace_id == workspace_id
            and a.pipeline_item_id == pipeline_id
            and a.pipeline_run_id == job_instance_id
        ]


class _MockSemanticModels:
    def __init__(self, scenario: ScenarioFixture) -> None:
        self._refreshes = scenario.refreshes

    async def get_refresh_history(self, workspace_id: str, semantic_model_id: str, *, top: int = 10) -> list[Refresh]:
        refreshes = [r for r in self._refreshes if r.workspace_id == workspace_id and r.item_id == semantic_model_id]
        refreshes.sort(key=lambda r: r.start_time.timestamp() if r.start_time else 0.0, reverse=True)
        return refreshes[:top]


class _MockMonitoring:
    """Workspace monitoring over the scenario's refreshes (the same facts the Power BI refresh history serves)."""

    def __init__(self, scenario: ScenarioFixture) -> None:
        self._refreshes = scenario.refreshes

    async def list_semantic_model_refreshes(
        self, workspace_id: str, *, since: datetime, until: datetime
    ) -> list[Refresh]:
        found = [
            r
            for r in self._refreshes
            if r.workspace_id == workspace_id
            and (when := r.end_time or r.start_time) is not None
            and since <= when <= until
        ]
        found.sort(key=lambda r: r.start_time.timestamp() if r.start_time else 0.0, reverse=True)
        return found


_KEPT_ACTIONS = frozenset({"metaData", "commitInfo", "protocol"})


class _MockDeltaLogs:
    def __init__(self, scenario: ScenarioFixture) -> None:
        self._logs: dict[tuple[str, str | None, str], DeltaLogFixture] = {
            (log.item_id, log.schema_name, log.table): log for log in scenario.delta_logs
        }

    def _log(self, table: DeltaTableRef) -> DeltaLogFixture:
        try:
            return self._logs[(table.item_id, table.schema_name, table.table)]
        except KeyError:
            raise FabricNotFoundError(
                f"No _delta_log recorded for {table.path} of {table.item_id}",
                status_code=404,
                error_code="PathNotFound",
            ) from None

    async def list_commits(self, table: DeltaTableRef) -> list[DeltaCommitRef]:
        return sorted(
            (DeltaCommitRef(version=c.version, modified_at=c.committed_at) for c in self._log(table).commits),
            key=lambda r: r.version,
        )

    async def read_commit(self, table: DeltaTableRef, version: int) -> DeltaCommitFile:
        for commit in self._log(table).commits:
            if commit.version == version:
                lines = [json.dumps(a, separators=(",", ":")) for a in commit.actions if set(a) & _KEPT_ACTIONS]
                return DeltaCommitFile(version=version, lines=lines)
        raise FabricNotFoundError(f"Delta commit {version} not found", status_code=404, error_code="PathNotFound")


class _MockCapacities:
    def __init__(self, estate: EstateFixture, scenario: ScenarioFixture) -> None:
        self._capacities = estate.capacities
        self._snapshots = scenario.capacity_snapshots

    async def list_capacities(self) -> list[Capacity]:
        return list(self._capacities)

    async def get_capacity_metrics(self, capacity_id: str, *, start: datetime, end: datetime) -> list[CapacitySnapshot]:
        if not any(c.id == capacity_id for c in self._capacities):
            raise FabricNotFoundError(
                f"Capacity {capacity_id} not found", status_code=404, error_code="CapacityNotFound"
            )
        return sorted(
            (s for s in self._snapshots if s.capacity_id == capacity_id and start <= s.timestamp <= end),
            key=lambda s: s.timestamp,
        )


class _MockDeployments:
    def __init__(self, estate: EstateFixture) -> None:
        self._states = estate.deployment_states

    async def get_deployment_states(self, item_id: str) -> list[DeploymentState]:
        logical = {s.logical_item_id for s in self._states if s.item_id == item_id}
        return [s for s in self._states if s.logical_item_id in logical]


class _MockDependencies:
    def __init__(self, estate: EstateFixture) -> None:
        self._deps = estate.dependencies

    async def list_dependencies(self) -> list[Dependency]:
        return list(self._deps)


class MockFabricClient:
    """Implements :class:`~orqentis.fabric.ports.FabricClient` over fixtures; no network access."""

    def __init__(self, estate: EstateFixture, scenario: ScenarioFixture) -> None:
        self.estate = estate
        self.scenario = scenario
        self._workspaces = _MockWorkspaces(estate)
        self._items = _MockItems(estate)
        self._jobs = _MockJobs(scenario)
        self._pipelines = _MockPipelines(scenario)
        self._semantic_models = _MockSemanticModels(scenario)
        self._capacities = _MockCapacities(estate, scenario)
        self._deployments = _MockDeployments(estate)
        self._dependencies = _MockDependencies(estate)
        self._monitoring = _MockMonitoring(scenario)
        self._delta_logs = _MockDeltaLogs(scenario)

    @property
    def workspaces(self) -> _MockWorkspaces:
        return self._workspaces

    @property
    def items(self) -> _MockItems:
        return self._items

    @property
    def jobs(self) -> _MockJobs:
        return self._jobs

    @property
    def pipelines(self) -> _MockPipelines:
        return self._pipelines

    @property
    def semantic_models(self) -> _MockSemanticModels:
        return self._semantic_models

    @property
    def capacities(self) -> _MockCapacities:
        return self._capacities

    @property
    def deployments(self) -> _MockDeployments:
        return self._deployments

    @property
    def dependencies(self) -> _MockDependencies:
        return self._dependencies

    @property
    def monitoring(self) -> _MockMonitoring:
        return self._monitoring

    @property
    def delta_logs(self) -> _MockDeltaLogs:
        return self._delta_logs

    async def aclose(self) -> None:
        return None
