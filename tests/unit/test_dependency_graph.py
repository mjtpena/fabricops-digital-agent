"""Dependency traversal, path finding and blast radius."""

from __future__ import annotations

from pathlib import Path

import pytest

from orqentis.domain.common import Environment, ItemType
from orqentis.domain.estate import DataProduct, Lakehouse, Notebook, Pipeline, Report, SemanticModel
from orqentis.domain.incidents import ImpactKind
from orqentis.domain.topology import Dependency, DependencyType
from orqentis.fabric.fixtures import load_estate
from orqentis.topology.catalog import EstateCatalog
from orqentis.topology.graph import DependencyGraph
from tests.conftest import REPO

T = "t1"


def edge(n: int, source: str, kind: DependencyType, target: str) -> Dependency:
    return Dependency(id=f"d{n}", tenant_id=T, source_item_id=source, target_item_id=target, dependency_type=kind)


@pytest.fixture
def catalog() -> EstateCatalog:
    c = EstateCatalog()
    for item in [
        Pipeline(id="pl", tenant_id=T, workspace_id="ws-a", display_name="pipeline", environment=Environment.PROD),
        Notebook(id="nb", tenant_id=T, workspace_id="ws-a", display_name="notebook", environment=Environment.PROD),
        Lakehouse(id="lh-bronze", tenant_id=T, workspace_id="ws-a", display_name="bronze"),
        Lakehouse(id="lh-gold", tenant_id=T, workspace_id="ws-a", display_name="gold"),
        SemanticModel(id="sm", tenant_id=T, workspace_id="ws-a", display_name="model"),
        SemanticModel(id="sm-b", tenant_id=T, workspace_id="ws-b", display_name="other-model"),
        Report(id="rpt", tenant_id=T, workspace_id="ws-a", display_name="report"),
        Report(id="rpt-b", tenant_id=T, workspace_id="ws-b", display_name="other-report"),
    ]:
        c.items[item.id] = item
    c.data_products["dp"] = DataProduct(
        id="dp", tenant_id=T, workspace_id="ws-a", display_name="gold_table", owner="owner@x"
    )
    return c


@pytest.fixture
def graph(catalog: EstateCatalog) -> DependencyGraph:
    return DependencyGraph(
        [
            edge(1, "pl", DependencyType.WRITES_TO, "lh-bronze"),
            edge(2, "pl", DependencyType.TRIGGERS, "nb"),
            edge(3, "nb", DependencyType.READS_FROM, "lh-bronze"),
            edge(4, "nb", DependencyType.WRITES_TO, "lh-gold"),
            edge(5, "lh-gold", DependencyType.PRODUCES, "dp"),
            edge(6, "sm", DependencyType.CONSUMES, "dp"),
            edge(7, "rpt", DependencyType.DEPENDS_ON, "sm"),
            edge(8, "sm-b", DependencyType.CONSUMES, "dp"),
            edge(9, "rpt-b", DependencyType.DEPENDS_ON, "sm-b"),
            edge(10, "nb", DependencyType.READS_FROM, "lh-gold"),  # cycle: nb <-> lh-gold
        ],
        catalog,
    )


@pytest.mark.parametrize(
    ("kind", "upstream_is_source"),
    [
        (DependencyType.TRIGGERS, True),
        (DependencyType.WRITES_TO, True),
        (DependencyType.REFRESHES, True),
        (DependencyType.PRODUCES, True),
        (DependencyType.READS_FROM, False),
        (DependencyType.DEPENDS_ON, False),
        (DependencyType.CONSUMES, False),
    ],
)
def test_edge_direction_semantics(kind: DependencyType, upstream_is_source: bool) -> None:
    dep = edge(1, "a", kind, "b")
    assert (dep.upstream_id, dep.downstream_id) == (("a", "b") if upstream_is_source else ("b", "a"))


def test_get_downstream_is_transitive_and_cycle_safe(graph: DependencyGraph) -> None:
    nodes = {n.item_id: n for n in graph.get_downstream("nb")}
    assert set(nodes) == {"lh-gold", "dp", "sm", "sm-b", "rpt", "rpt-b"}
    assert nodes["lh-gold"].depth == 1
    assert nodes["rpt"].path == ("nb", "lh-gold", "dp", "sm", "rpt")


def test_get_upstream(graph: DependencyGraph) -> None:
    assert {n.item_id for n in graph.get_upstream("rpt")} == {"sm", "dp", "lh-gold", "nb", "pl", "lh-bronze"}
    assert {n.item_id for n in graph.get_upstream("rpt", max_depth=2)} == {"sm", "dp"}


def test_find_path_follows_data_flow_only(graph: DependencyGraph) -> None:
    assert graph.find_path("pl", "rpt-b") == ["pl", "nb", "lh-gold", "dp", "sm-b", "rpt-b"]
    assert graph.find_path("rpt", "pl") is None
    assert graph.find_path("sm", "sm") == ["sm"]


def test_blast_radius_classifies_impact_and_cross_workspace(graph: DependencyGraph) -> None:
    radius = graph.get_blast_radius("nb")
    by_id = {a.item_id: a for a in radius.assets}
    assert by_id["nb"].impact is ImpactKind.FAILURE_POINT
    assert by_id["lh-gold"].impact is ImpactKind.DIRECT
    assert by_id["rpt-b"].impact is ImpactKind.INDIRECT
    assert radius.total == 7
    assert radius.cross_workspace is True
    assert radius.data_product_ids == ["dp"]
    assert radius.by_type == {"DataProduct": 1, "Lakehouse": 1, "Report": 2, "SemanticModel": 2}
    assert "lh-bronze" not in by_id  # upstream of the failure point is not impacted


def test_blast_radius_includes_also_impacted_items(graph: DependencyGraph) -> None:
    radius = graph.get_blast_radius(["lh-bronze"], also_impacted=["sm"])
    by_id = {a.item_id: a for a in radius.assets}
    assert by_id["sm"].depth == 1
    assert by_id["rpt"].depth == 2


def test_blast_radius_of_leaf_is_empty(graph: DependencyGraph) -> None:
    radius = graph.get_blast_radius("rpt")
    assert [a.item_id for a in radius.assets] == ["rpt"]
    assert radius.cross_workspace is False


def test_synthetic_estate_blast_radius_matches_spec() -> None:
    estate = load_estate(REPO / "fixtures")
    catalog = EstateCatalog()
    catalog.items = {i.id: i for i in [*estate.items, *estate.external_sources]}
    catalog.data_products = {d.id: d for d in estate.data_products}
    catalog.workspaces = {w.id: w for w in estate.workspaces}
    graph = DependencyGraph(estate.dependencies, catalog)
    names = {a.display_name for a in graph.get_blast_radius("nb-transform-sales").assets if a.depth > 0}
    assert {"sales_gold", "SalesSemanticModel", "ExecutiveSalesReport"} <= names
    assert {"FinanceSemanticModel", "MonthEndCloseReport"} <= names  # cross-workspace consumers
    upstream = {n.item_id for n in graph.get_upstream("nb-transform-sales")}
    assert {"lh-sales-bronze", "src-crm-sales-orders"} <= upstream
    assert catalog.node("dp-sales-gold") is not None
    assert catalog.node_or_placeholder("missing").item_type is ItemType.OTHER


def test_fixture_dependency_ids_are_unique() -> None:
    estate = load_estate(Path(REPO / "fixtures"))
    ids = [d.id for d in estate.dependencies]
    assert len(ids) == len(set(ids))
