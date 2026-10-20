"""In-process catalog of the estate: workspaces, items, data products, SLAs, capacities.

Fabric item APIs are workspace-scoped; the catalog lets the rest of the system resolve an
item id to its workspace, type, owner and environment without extra round trips.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from orqentis.domain.common import Environment, ItemType
from orqentis.domain.estate import Capacity, DataProduct, FabricItem, Workspace
from orqentis.domain.sla import SLA
from orqentis.fabric.fixtures import EstateContracts
from orqentis.fabric.ports import FabricClient
from orqentis.observability.logging import get_logger

_log = get_logger(__name__)


class EntityDiff(BaseModel):
    """Ids that appeared, disappeared or changed between two snapshots of one kind of entity."""

    model_config = ConfigDict(extra="forbid")

    added: list[str] = Field(default_factory=list)
    removed: list[str] = Field(default_factory=list)
    # id -> names of the fields whose value changed
    changed: dict[str, list[str]] = Field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not (self.added or self.removed or self.changed)


class CatalogDiff(BaseModel):
    """What a catalog refresh changed (ids only: display names are untrusted Fabric metadata)."""

    model_config = ConfigDict(extra="forbid")

    workspaces: EntityDiff = Field(default_factory=EntityDiff)
    items: EntityDiff = Field(default_factory=EntityDiff)
    capacities: EntityDiff = Field(default_factory=EntityDiff)
    data_products: EntityDiff = Field(default_factory=EntityDiff)
    slas: EntityDiff = Field(default_factory=EntityDiff)
    dependencies: EntityDiff = Field(default_factory=EntityDiff)

    @property
    def is_empty(self) -> bool:
        return all(d.is_empty for d in self._parts().values())

    def _parts(self) -> dict[str, EntityDiff]:
        return {name: getattr(self, name) for name in type(self).model_fields}

    def counts(self) -> dict[str, dict[str, int]]:
        """``{kind: {added, removed, changed}}`` for the kinds that changed."""
        return {
            kind: {"added": len(d.added), "removed": len(d.removed), "changed": len(d.changed)}
            for kind, d in self._parts().items()
            if not d.is_empty
        }

    def summary(self, *, limit: int = 25) -> dict[str, Any]:
        """Audit/log friendly view: counts plus a bounded number of ids per kind."""
        return {
            kind: {
                "added": d.added[:limit],
                "removed": d.removed[:limit],
                "changed": {k: v for k, v in list(d.changed.items())[:limit]},
                "counts": {"added": len(d.added), "removed": len(d.removed), "changed": len(d.changed)},
            }
            for kind, d in self._parts().items()
            if not d.is_empty
        }


def diff_entities(before: Mapping[str, BaseModel], after: Mapping[str, BaseModel]) -> EntityDiff:
    changed: dict[str, list[str]] = {}
    for key in before.keys() & after.keys():
        if before[key] == after[key]:
            continue
        old, new = before[key].model_dump(mode="json"), after[key].model_dump(mode="json")
        changed[key] = sorted(f for f in old.keys() | new.keys() if old.get(f) != new.get(f))
    return EntityDiff(
        added=sorted(after.keys() - before.keys()),
        removed=sorted(before.keys() - after.keys()),
        changed=dict(sorted(changed.items())),
    )


@dataclass(frozen=True)
class NodeInfo:
    """Display metadata for any node in the dependency graph (Fabric item or logical node)."""

    id: str
    item_type: ItemType
    display_name: str
    workspace_id: str | None
    environment: Environment
    owner: str | None


@dataclass
class EstateCatalog:
    workspaces: dict[str, Workspace] = field(default_factory=dict)
    items: dict[str, FabricItem] = field(default_factory=dict)
    data_products: dict[str, DataProduct] = field(default_factory=dict)
    slas: dict[str, SLA] = field(default_factory=dict)
    capacities: dict[str, Capacity] = field(default_factory=dict)

    @classmethod
    async def load(cls, fabric: FabricClient, contracts: EstateContracts) -> EstateCatalog:
        catalog = cls()
        for capacity in await fabric.capacities.list_capacities():
            catalog.capacities[capacity.id] = capacity
        for workspace in await fabric.workspaces.list_workspaces():
            catalog.workspaces[workspace.id] = workspace
            for item in await fabric.items.list_items(workspace.id):
                catalog.items[item.id] = item
        for source in contracts.external_sources:
            catalog.items.setdefault(source.id, source)
        catalog.data_products = {dp.id: dp for dp in contracts.data_products}
        catalog.slas = {s.id: s for s in contracts.slas}
        _log.info(
            "estate catalog loaded",
            extra={
                "workspaces": len(catalog.workspaces),
                "items": len(catalog.items),
                "data_products": len(catalog.data_products),
            },
        )
        return catalog

    def node(self, node_id: str) -> NodeInfo | None:
        if (item := self.items.get(node_id)) is not None:
            return NodeInfo(
                item.id,
                item.item_type,
                item.display_name,
                item.workspace_id,
                item.environment,
                item.owner or self._workspace_owner(item.workspace_id),
            )
        if (dp := self.data_products.get(node_id)) is not None:
            return NodeInfo(dp.id, ItemType.DATA_PRODUCT, dp.display_name, dp.workspace_id, dp.environment, dp.owner)
        return None

    def node_or_placeholder(self, node_id: str) -> NodeInfo:
        return self.node(node_id) or NodeInfo(node_id, ItemType.OTHER, node_id, None, Environment.UNKNOWN, None)

    def _workspace_owner(self, workspace_id: str) -> str | None:
        ws = self.workspaces.get(workspace_id)
        return ws.owner if ws else None

    def workspace_of(self, item_id: str) -> str | None:
        node = self.node(item_id)
        return node.workspace_id if node else None

    def capacity_of_workspace(self, workspace_id: str) -> Capacity | None:
        ws = self.workspaces.get(workspace_id)
        return self.capacities.get(ws.capacity_id) if ws and ws.capacity_id else None

    def slas_for_data_product(self, data_product_id: str) -> list[SLA]:
        return [s for s in self.slas.values() if s.data_product_id == data_product_id]

    def find_items_by_name(self, display_name: str) -> list[FabricItem]:
        return [i for i in self.items.values() if i.display_name == display_name]

    def diff(self, other: EstateCatalog) -> CatalogDiff:
        """Changes that moving from this catalog to ``other`` would make (dependencies are diffed separately)."""
        return CatalogDiff(
            workspaces=diff_entities(self.workspaces, other.workspaces),
            items=diff_entities(self.items, other.items),
            capacities=diff_entities(self.capacities, other.capacities),
            data_products=diff_entities(self.data_products, other.data_products),
            slas=diff_entities(self.slas, other.slas),
        )

    def apply(self, other: EstateCatalog) -> None:
        """Adopt ``other``'s contents in place.

        The dictionaries are *rebound*, not mutated: a collector that is iterating ``catalog.items`` across an
        ``await`` keeps its consistent snapshot, and every holder of this catalog sees the new one on its next
        access. There is no ``await`` here, so no coroutine ever observes a half-applied catalog.
        """
        self.workspaces = other.workspaces
        self.items = other.items
        self.data_products = other.data_products
        self.slas = other.slas
        self.capacities = other.capacities
