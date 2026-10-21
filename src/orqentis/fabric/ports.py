"""Ports for Fabric access.

READ and WRITE are separate capability boundaries:

* :class:`FabricClient` exposes read-only services and is what tools and collectors receive.
* :class:`FabricWriteClient` performs mutations and is only ever handed to the
  :class:`~orqentis.actions.gateway.ActionGateway`, which enforces policy and approval.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from orqentis.domain.actions import Action
from orqentis.domain.approvals import ApprovalRequest
from orqentis.domain.common import ItemType
from orqentis.domain.delta import DeltaCommitFile, DeltaCommitRef, DeltaTableRef
from orqentis.domain.deployment import DeploymentState
from orqentis.domain.estate import Capacity, FabricItem, Workspace
from orqentis.domain.runs import ActivityRun, CapacitySnapshot, PipelineRun, Refresh
from orqentis.domain.topology import Dependency


class WorkspaceService(Protocol):
    async def list_workspaces(self) -> list[Workspace]: ...

    async def get_workspace(self, workspace_id: str) -> Workspace: ...


class ItemService(Protocol):
    async def list_items(self, workspace_id: str, *, item_type: ItemType | None = None) -> list[FabricItem]: ...

    async def get_item(self, workspace_id: str, item_id: str) -> FabricItem: ...


class JobService(Protocol):
    async def list_job_instances(
        self, workspace_id: str, item_id: str, *, limit: int = 50, since: datetime | None = None
    ) -> list[PipelineRun]:
        """Most recent runs first. ``since`` drops runs that finished (or, if unfinished, started) before it."""
        ...

    async def get_job_instance(self, workspace_id: str, item_id: str, job_instance_id: str) -> PipelineRun: ...


class PipelineService(Protocol):
    async def query_activity_runs(
        self, workspace_id: str, pipeline_id: str, job_instance_id: str
    ) -> list[ActivityRun]: ...


class SemanticModelService(Protocol):
    async def get_refresh_history(
        self, workspace_id: str, semantic_model_id: str, *, top: int = 10
    ) -> list[Refresh]: ...


class CapacityService(Protocol):
    async def list_capacities(self) -> list[Capacity]: ...

    async def get_capacity_metrics(
        self, capacity_id: str, *, start: datetime, end: datetime
    ) -> list[CapacitySnapshot]: ...


class MonitoringService(Protocol):
    """Item logs from Fabric *workspace monitoring* (the monitoring Eventhouse of a workspace), read-only.

    The source of semantic model refresh outcomes when the Power BI refresh history is unavailable to the reader
    (it needs dataset Write; docs/security.md section 11, ADR-0020).
    """

    async def list_semantic_model_refreshes(
        self, workspace_id: str, *, since: datetime, until: datetime
    ) -> list[Refresh]:
        """Refreshes that ended (or, if unfinished, started) in ``[since, until]``, newest first.

        Raises :class:`~orqentis.fabric.errors.FabricCapabilityUnavailableError` when the workspace has no readable
        monitoring database (monitoring not enabled, or not visible to the reader).
        """
        ...


class DeltaLogService(Protocol):
    """Read-only access to a declared Delta table's ``_delta_log`` (OneLake ADLS Gen2 API)."""

    async def list_commits(self, table: DeltaTableRef) -> list[DeltaCommitRef]:
        """The JSON commit files in the table's log, by ascending version (checkpoints are not commits)."""
        ...

    async def read_commit(self, table: DeltaTableRef, version: int) -> DeltaCommitFile:
        """The ``metaData`` / ``commitInfo`` / ``protocol`` action lines of one commit, size-capped."""
        ...


class DeploymentService(Protocol):
    async def get_deployment_states(self, item_id: str) -> list[DeploymentState]:
        """Deployment state of the logical item behind ``item_id`` in every stage."""
        ...


class DependencyService(Protocol):
    async def list_dependencies(self) -> list[Dependency]: ...


class FabricClient(Protocol):
    """Read-only access to a Fabric estate."""

    @property
    def workspaces(self) -> WorkspaceService: ...

    @property
    def items(self) -> ItemService: ...

    @property
    def jobs(self) -> JobService: ...

    @property
    def pipelines(self) -> PipelineService: ...

    @property
    def semantic_models(self) -> SemanticModelService: ...

    @property
    def capacities(self) -> CapacityService: ...

    @property
    def deployments(self) -> DeploymentService: ...

    @property
    def monitoring(self) -> MonitoringService: ...

    @property
    def delta_logs(self) -> DeltaLogService: ...

    @property
    def dependencies(self) -> DependencyService: ...

    async def aclose(self) -> None: ...


class FabricWriteClient(Protocol):
    """Mutation capability. Requires an APPROVED approval bound to the exact action."""

    async def execute(self, action: Action, *, approval: ApprovalRequest) -> dict[str, Any]: ...
