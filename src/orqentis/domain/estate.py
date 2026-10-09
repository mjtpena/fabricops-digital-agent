"""Fabric estate: tenants, capacities, workspaces, items and data products."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from orqentis.domain.common import DomainModel, Environment, ItemType, Metadata


class Tenant(DomainModel):
    id: str
    display_name: str
    source: str = "fixture"
    metadata: Metadata = Field(default_factory=dict)


class Capacity(DomainModel):
    id: str
    tenant_id: str
    display_name: str
    sku: str
    region: str | None = None
    state: str = "Active"
    environment: Environment = Environment.UNKNOWN
    source: str = "fabric"
    metadata: Metadata = Field(default_factory=dict)


class Workspace(DomainModel):
    id: str
    tenant_id: str
    display_name: str
    capacity_id: str | None = None
    environment: Environment = Environment.UNKNOWN
    description: str | None = None
    owner: str | None = None
    source: str = "fabric"
    metadata: Metadata = Field(default_factory=dict)


class _ItemBase(DomainModel):
    id: str
    tenant_id: str
    workspace_id: str
    display_name: str
    description: str | None = None
    environment: Environment = Environment.UNKNOWN
    owner: str | None = None
    source: str = "fabric"
    tags: list[str] = Field(default_factory=list)
    metadata: Metadata = Field(default_factory=dict)


class PipelineActivity(DomainModel):
    """Static definition of a pipeline activity and the item it acts on (if any)."""

    name: str
    activity_type: str
    target_item_id: str | None = None
    depends_on: list[str] = Field(default_factory=list)


class Pipeline(_ItemBase):
    item_type: Literal[ItemType.DATA_PIPELINE] = ItemType.DATA_PIPELINE
    activities: list[PipelineActivity] = Field(default_factory=list)
    schedule: str | None = None


class Notebook(_ItemBase):
    item_type: Literal[ItemType.NOTEBOOK] = ItemType.NOTEBOOK
    default_lakehouse_id: str | None = None


class Lakehouse(_ItemBase):
    item_type: Literal[ItemType.LAKEHOUSE] = ItemType.LAKEHOUSE
    tables: list[str] = Field(default_factory=list)


class Warehouse(_ItemBase):
    item_type: Literal[ItemType.WAREHOUSE] = ItemType.WAREHOUSE


class SemanticModel(_ItemBase):
    item_type: Literal[ItemType.SEMANTIC_MODEL] = ItemType.SEMANTIC_MODEL
    storage_mode: str | None = None
    refresh_schedule: str | None = None


class Report(_ItemBase):
    item_type: Literal[ItemType.REPORT] = ItemType.REPORT
    semantic_model_id: str | None = None


class ExternalSource(_ItemBase):
    """A system outside Fabric that feeds the estate (declared, not discovered)."""

    item_type: Literal[ItemType.EXTERNAL_SOURCE] = ItemType.EXTERNAL_SOURCE
    source: str = "declared"


class GenericItem(_ItemBase):
    item_type: Literal[ItemType.OTHER] = ItemType.OTHER
    fabric_type: str | None = None


FabricItem = Annotated[
    Pipeline | Notebook | Lakehouse | Warehouse | SemanticModel | Report | ExternalSource | GenericItem,
    Field(discriminator="item_type"),
]
"""Any item in the estate catalog, discriminated on ``item_type``."""


class DataProduct(DomainModel):
    """A logical, owned data asset (e.g. the ``sales_gold`` table) backed by Fabric items."""

    id: str
    tenant_id: str
    workspace_id: str
    display_name: str
    owner: str
    description: str | None = None
    environment: Environment = Environment.UNKNOWN
    backing_item_ids: list[str] = Field(default_factory=list)
    item_type: Literal[ItemType.DATA_PRODUCT] = ItemType.DATA_PRODUCT
    source: str = "declared"
    metadata: Metadata = Field(default_factory=dict)
