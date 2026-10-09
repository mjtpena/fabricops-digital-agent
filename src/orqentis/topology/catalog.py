"""In-process catalog of the estate: workspaces, items, data products, SLAs, capacities.

Fabric item APIs are workspace-scoped; the catalog lets the rest of the system resolve an
item id to its workspace, type, owner and environment without extra round trips.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from orqentis.domain.common import Environment, ItemType
from orqentis.domain.estate import Capacity, DataProduct, FabricItem, Workspace
from orqentis.domain.sla import SLA
from orqentis.fabric.fixtures import EstateContracts
from orqentis.fabric.ports import FabricClient
from orqentis.observability.logging import get_logger

_log = get_logger(__name__)


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
