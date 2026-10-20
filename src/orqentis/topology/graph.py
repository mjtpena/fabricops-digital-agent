"""Dependency graph with traversal, blast radius and path finding.

The graph is built from :class:`Dependency` edges supplied by a :class:`DependencyStore`
(SQLite in the MVP); the traversal logic is independent of how edges are stored.
"""

from __future__ import annotations

from collections import Counter, deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol

from orqentis.domain.common import ItemType
from orqentis.domain.incidents import AffectedAsset, BlastRadius, ImpactKind
from orqentis.domain.topology import Dependency
from orqentis.topology.catalog import EstateCatalog


class DependencyStore(Protocol):
    async def list_edges(self) -> list[Dependency]: ...

    async def replace_all(self, edges: Sequence[Dependency]) -> None: ...


@dataclass(frozen=True)
class GraphNode:
    item_id: str
    depth: int
    path: tuple[str, ...]
    via: Dependency | None


class DependencyGraph:
    def __init__(self, edges: Iterable[Dependency], catalog: EstateCatalog) -> None:
        self._catalog = catalog
        self._edges: list[Dependency] = []
        self._down: dict[str, list[Dependency]] = {}
        self._up: dict[str, list[Dependency]] = {}
        self.replace_edges(edges)

    def replace_edges(self, edges: Iterable[Dependency]) -> None:
        """Swap in a new edge set. The indexes are rebuilt aside and bound at the end, with no ``await`` between."""
        new_edges = list(edges)
        down: dict[str, list[Dependency]] = {}
        up: dict[str, list[Dependency]] = {}
        for edge in new_edges:
            down.setdefault(edge.upstream_id, []).append(edge)
            up.setdefault(edge.downstream_id, []).append(edge)
        self._edges, self._down, self._up = new_edges, down, up

    @classmethod
    async def from_store(cls, store: DependencyStore, catalog: EstateCatalog) -> DependencyGraph:
        return cls(await store.list_edges(), catalog)

    @property
    def edges(self) -> list[Dependency]:
        return list(self._edges)

    def edges_for(self, item_id: str) -> list[Dependency]:
        return [*self._up.get(item_id, []), *self._down.get(item_id, [])]

    def _walk(
        self, start_ids: Iterable[str], *, downstream: bool, max_depth: int | None, direct_ids: Iterable[str] = ()
    ) -> list[GraphNode]:
        """Breadth-first traversal. ``direct_ids`` are seeded at depth 1 (known direct impacts)."""
        adjacency = self._down if downstream else self._up
        starts = list(dict.fromkeys(start_ids))
        seen: set[str] = set(starts)
        queue: deque[GraphNode] = deque(GraphNode(s, 0, (s,), None) for s in starts)
        found: list[GraphNode] = []
        for direct in dict.fromkeys(direct_ids):
            if direct not in seen:
                seen.add(direct)
                node = GraphNode(direct, 1, (direct,), None)
                found.append(node)
                queue.append(node)
        while queue:
            current = queue.popleft()
            if max_depth is not None and current.depth >= max_depth:
                continue
            for edge in adjacency.get(current.item_id, []):
                nxt = edge.downstream_id if downstream else edge.upstream_id
                if nxt in seen:
                    continue  # cycle-safe; BFS keeps the shortest path
                seen.add(nxt)
                node = GraphNode(nxt, current.depth + 1, (*current.path, nxt), edge)
                found.append(node)
                queue.append(node)
        return found

    def get_upstream(self, item_id: str, *, max_depth: int | None = None) -> list[GraphNode]:
        return self._walk([item_id], downstream=False, max_depth=max_depth)

    def get_downstream(self, item_id: str, *, max_depth: int | None = None) -> list[GraphNode]:
        return self._walk([item_id], downstream=True, max_depth=max_depth)

    def find_path(self, source: str, target: str) -> list[str] | None:
        """Shortest path following data flow (``source`` upstream of ``target``), else ``None``."""
        if source == target:
            return [source]
        for node in self._walk([source], downstream=True, max_depth=None):
            if node.item_id == target:
                return list(node.path)
        return None

    def get_blast_radius(self, item_ids: str | Sequence[str], *, also_impacted: Sequence[str] = ()) -> BlastRadius:
        """Everything downstream of the failure point(s).

        ``also_impacted`` adds items known to be directly impacted even if the graph does not say so
        (e.g. targets of pipeline activities skipped because of the failure).
        """
        origins = [item_ids] if isinstance(item_ids, str) else list(item_ids)
        nodes = self._walk(origins, downstream=True, max_depth=None, direct_ids=also_impacted)

        assets: list[AffectedAsset] = []
        for origin in origins:
            info = self._catalog.node_or_placeholder(origin)
            assets.append(
                AffectedAsset(
                    item_id=origin,
                    item_type=info.item_type,
                    display_name=info.display_name,
                    workspace_id=info.workspace_id,
                    environment=info.environment,
                    owner=info.owner,
                    depth=0,
                    impact=ImpactKind.FAILURE_POINT,
                    path=[origin],
                )
            )
        for node in sorted(nodes, key=lambda n: (n.depth, n.item_id)):
            info = self._catalog.node_or_placeholder(node.item_id)
            assets.append(
                AffectedAsset(
                    item_id=node.item_id,
                    item_type=info.item_type,
                    display_name=info.display_name,
                    workspace_id=info.workspace_id,
                    environment=info.environment,
                    owner=info.owner,
                    depth=node.depth,
                    impact=ImpactKind.DIRECT if node.depth == 1 else ImpactKind.INDIRECT,
                    path=list(node.path),
                )
            )
        downstream = [a for a in assets if a.impact is not ImpactKind.FAILURE_POINT]
        by_type = Counter(a.item_type.value for a in downstream)
        origin_workspaces = {a.workspace_id for a in assets if a.impact is ImpactKind.FAILURE_POINT}
        workspaces = sorted({a.workspace_id for a in assets if a.workspace_id})
        data_products = [a.item_id for a in downstream if a.item_type is ItemType.DATA_PRODUCT]
        cross = any(a.workspace_id and a.workspace_id not in origin_workspaces for a in downstream)
        names = ", ".join(a.display_name for a in downstream) or "none"
        return BlastRadius(
            origin_item_ids=origins,
            assets=assets,
            by_type=dict(sorted(by_type.items())),
            workspace_ids=workspaces,
            data_product_ids=data_products,
            max_depth=max((a.depth for a in assets), default=0),
            cross_workspace=cross,
            summary=f"{len(downstream)} downstream asset(s) across {len(workspaces)} workspace(s): {names}",
        )
