"""Estate endpoints (read-only views over the catalog and dependency graph)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, status

from orqentis.api.authz import ReadPrincipal
from orqentis.api.deps import ContainerDep
from orqentis.api.schemas import DependencyNode, ItemDependencies
from orqentis.domain.estate import Workspace
from orqentis.topology.graph import GraphNode

router = APIRouter(prefix="/estate", tags=["estate"])


@router.get("/workspaces", response_model=list[Workspace])
async def list_workspaces(c: ContainerDep, _: ReadPrincipal) -> list[Workspace]:
    return sorted(c.catalog.workspaces.values(), key=lambda w: w.display_name)


@router.get("/items/{item_id}/dependencies", response_model=ItemDependencies)
async def item_dependencies(
    item_id: str,
    c: ContainerDep,
    _: ReadPrincipal,
    max_depth: int | None = Query(default=None, ge=1, le=10),
) -> ItemDependencies:
    node = c.catalog.node(item_id)
    if node is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Item {item_id} not found")

    def view(n: GraphNode) -> DependencyNode:
        info = c.catalog.node_or_placeholder(n.item_id)
        return DependencyNode(
            item_id=n.item_id,
            display_name=info.display_name,
            item_type=info.item_type,
            workspace_id=info.workspace_id,
            depth=n.depth,
            path=list(n.path),
            via=n.via.dependency_type.value if n.via else None,
        )

    return ItemDependencies(
        item_id=item_id,
        display_name=node.display_name,
        item_type=node.item_type,
        upstream=[view(n) for n in c.graph.get_upstream(item_id, max_depth=max_depth)],
        downstream=[view(n) for n in c.graph.get_downstream(item_id, max_depth=max_depth)],
        blast_radius=c.graph.get_blast_radius(item_id),
    )
