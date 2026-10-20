"""Fabric estate: tenants, capacities, workspaces, items and data products."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import ConfigDict, Field, JsonValue, model_validator

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


_ENVIRONMENT_SELECTOR_SCHEMA: dict[str, JsonValue] = {
    # Exactly one selector is present (a null value does not count as present).
    "oneOf": [
        {"required": [key], "properties": {key: {"type": "string"}}}
        for key in ("workspace_id", "workspace_name_pattern", "item_id")
    ]
}


class EnvironmentDeclaration(DomainModel):
    """A declared environment that takes precedence over inference from names.

    Exactly one selector identifies what is classified: a workspace by ``workspace_id``, workspaces by
    ``workspace_name_pattern`` (``*`` and ``?`` wildcards, case-insensitive; every other character is
    literal, so ``Sales [PROD]`` matches that name exactly) or one item by ``item_id``. Items inherit the
    environment of their workspace unless an ``item_id`` declaration says otherwise.

    ``inferred`` marks a draft entry that ``orqentis contracts scaffold`` pre-filled from naming
    conventions: it is applied like any other declaration, but ``contracts validate`` warns until a human
    reviews it and clears the flag.
    """

    model_config = ConfigDict(json_schema_extra=_ENVIRONMENT_SELECTOR_SCHEMA)  # merged with DomainModel's

    workspace_id: str | None = Field(default=None, min_length=1, max_length=128)
    workspace_name_pattern: str | None = Field(default=None, min_length=1, max_length=128)
    item_id: str | None = Field(default=None, min_length=1, max_length=128)
    environment: Environment
    inferred: bool = False
    note: str | None = Field(default=None, max_length=300)

    @model_validator(mode="after")
    def _exactly_one_selector(self) -> Self:
        selectors = [s for s in (self.workspace_id, self.workspace_name_pattern, self.item_id) if s is not None]
        if len(selectors) != 1:
            raise ValueError("declare exactly one of workspace_id, workspace_name_pattern or item_id")
        return self
