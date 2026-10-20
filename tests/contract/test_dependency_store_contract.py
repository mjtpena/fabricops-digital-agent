"""DependencyStore contract: the edge set is replaced as a whole."""

from __future__ import annotations

from orqentis.domain.topology import Dependency, DependencyType
from orqentis.topology.graph import DependencyStore


def edge(n: int, source: str = "a", target: str = "b", kind: DependencyType = DependencyType.TRIGGERS) -> Dependency:
    return Dependency(id=f"d{n}", tenant_id="t", source_item_id=source, target_item_id=target, dependency_type=kind)


async def test_replace_all_replaces_and_lists_in_id_order(dependency_store: DependencyStore) -> None:
    assert await dependency_store.list_edges() == []
    await dependency_store.replace_all([edge(2, "b", "c"), edge(1, "a", "b")])
    assert [e.id for e in await dependency_store.list_edges()] == ["d1", "d2"]
    await dependency_store.replace_all([edge(3, "x", "y", DependencyType.READS_FROM)])
    assert [e.id for e in await dependency_store.list_edges()] == ["d3"]
    await dependency_store.replace_all([])
    assert await dependency_store.list_edges() == []


async def test_edges_round_trip(dependency_store: DependencyStore) -> None:
    original = edge(1).model_copy(update={"source": "declared", "metadata": {"note": "x", "n": 2}})
    await dependency_store.replace_all([original])
    assert await dependency_store.list_edges() == [original]
