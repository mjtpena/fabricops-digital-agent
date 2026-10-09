"""Deployment state of an item across DEV / TEST / PROD."""

from __future__ import annotations

from pydantic import AwareDatetime, Field

from orqentis.domain.common import DomainModel, Environment, ItemType, Metadata


class DeploymentState(DomainModel):
    id: str
    tenant_id: str
    logical_item_id: str
    item_id: str
    workspace_id: str
    item_type: ItemType
    display_name: str
    environment: Environment
    deployed_at: AwareDatetime | None = None
    commit_id: str | None = None
    config_hash: str | None = None
    deployed_by: str | None = None
    source: str = "fabric.deployment-pipelines"
    metadata: Metadata = Field(default_factory=dict)


class DeploymentComparison(DomainModel):
    """Result of comparing one logical item across stages (deterministic)."""

    logical_item_id: str
    item_id: str
    states: list[DeploymentState]
    drift_detected: bool
    differences: list[str] = Field(default_factory=list)
    last_production_change: AwareDatetime | None = None
    changed_within_window: bool = False
    window_hours: int
