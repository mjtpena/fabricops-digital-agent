"""Catalog refresh with diffing: new estate objects become visible without a restart."""

from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest

from orqentis.bootstrap import Container
from orqentis.domain.common import Environment
from orqentis.domain.estate import Lakehouse, Notebook, Workspace
from orqentis.domain.topology import Dependency, DependencyType
from orqentis.fabric.mock import MockFabricClient
from orqentis.topology.refresh import CatalogRefreshRefusedError

NEW_ITEM = "lh-sales-platinum"
NEW_EDGE = "dep-new-platinum"


def mock_estate(container: Container) -> MockFabricClient:
    assert isinstance(container.fabric, MockFabricClient)
    return container.fabric


def add_platinum_lakehouse(container: Container) -> None:
    """A lakehouse somebody created in Fabric after start-up, fed by the existing silver lakehouse."""
    estate = mock_estate(container).estate
    estate.items.append(
        Lakehouse(
            id=NEW_ITEM,
            tenant_id="tenant-contoso",
            workspace_id="ws-sales-prod",
            display_name="lh_sales_platinum",
            environment=Environment.PROD,
        )
    )
    estate.dependencies.append(
        Dependency(
            id=NEW_EDGE,
            tenant_id="tenant-contoso",
            source_item_id="nb-transform-sales",
            target_item_id=NEW_ITEM,
            dependency_type=DependencyType.WRITES_TO,
        )
    )


async def test_an_item_added_between_refreshes_appears_in_the_catalog_and_graph_with_a_reported_diff(
    container: Container,
) -> None:
    assert NEW_ITEM not in container.catalog.items
    assert NEW_ITEM not in {n.item_id for n in container.graph.get_downstream("nb-transform-sales")}

    add_platinum_lakehouse(container)
    diff = await container.catalog_refresher.refresh()

    # The catalog and the graph both see the new item...
    assert NEW_ITEM in container.catalog.items
    assert container.catalog.node(NEW_ITEM) is not None
    assert NEW_ITEM in {n.item_id for n in container.graph.get_downstream("nb-transform-sales")}
    radius = container.graph.get_blast_radius("nb-transform-sales")
    assert NEW_ITEM in {a.item_id for a in radius.assets}
    # ...the dependency store was updated too, so a restart keeps it...
    with closing(sqlite3.connect(container.settings.state_db_path)) as db:
        assert NEW_EDGE in {row[0] for row in db.execute("SELECT id FROM dependencies")}
    # ...and the diff says exactly what happened.
    assert diff.items.added == [NEW_ITEM] and diff.items.removed == [] and diff.items.changed == {}
    assert diff.dependencies.added == [NEW_EDGE]
    assert diff.workspaces.is_empty and diff.capacities.is_empty and not diff.is_empty
    assert diff.counts() == {
        "items": {"added": 1, "removed": 0, "changed": 0},
        "dependencies": {"added": 1, "removed": 0, "changed": 0},
    }

    records = [r for r in await container.audit.list() if r.action == "catalog.refresh"]
    assert len(records) == 1 and records[0].actor == "system:orqentis"
    assert records[0].details["diff"]["items"]["added"] == [NEW_ITEM]


async def test_a_refresh_without_changes_is_silent(container: Container) -> None:
    diff = await container.catalog_refresher.refresh()
    assert diff.is_empty and diff.counts() == {}
    assert [r for r in await container.audit.list() if r.action == "catalog.refresh"] == []


async def test_removed_and_changed_items_are_reported(container: Container) -> None:
    estate = mock_estate(container).estate
    victim = next(i for i in estate.items if isinstance(i, Lakehouse))
    renamed = next(i for i in estate.items if isinstance(i, Notebook))
    estate.items.remove(victim)
    estate.items[estate.items.index(renamed)] = renamed.model_copy(update={"display_name": "Transform Sales v2"})

    diff = await container.catalog_refresher.refresh()
    assert diff.items.removed == [victim.id]
    assert diff.items.changed == {renamed.id: ["display_name"]}
    assert victim.id not in container.catalog.items
    assert container.catalog.items[renamed.id].display_name == "Transform Sales v2"


async def test_a_collector_iterating_the_catalog_keeps_a_consistent_snapshot_across_a_refresh(
    container: Container,
) -> None:
    snapshot = container.catalog.items.values()
    before = len(container.catalog.items)
    add_platinum_lakehouse(container)
    await container.catalog_refresher.refresh()
    # The view taken before the refresh is untouched (no "dictionary changed size during iteration").
    assert len(list(snapshot)) == before
    assert len(container.catalog.items) == before + 1


async def test_a_suspicious_empty_estate_is_refused_and_the_live_catalog_is_kept(
    container: Container, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def no_workspaces() -> list[Workspace]:
        return []  # e.g. the identity lost access, or a bad token: Fabric answers with an empty list

    monkeypatch.setattr(container.fabric.workspaces, "list_workspaces", no_workspaces)
    workspaces, items = dict(container.catalog.workspaces), dict(container.catalog.items)
    with pytest.raises(CatalogRefreshRefusedError, match="no workspaces"):
        await container.catalog_refresher.refresh()
    assert container.catalog.workspaces == workspaces and container.catalog.items == items


async def test_the_scheduled_job_applies_the_refresh(container: Container) -> None:
    from datetime import timedelta

    from orqentis.config.clock import FixedClock

    clock = container.clock
    assert isinstance(clock, FixedClock)
    await container.scheduler.tick()  # minute 0: catalog-refresh waits one interval before its first run
    await container.scheduler.wait_idle()
    add_platinum_lakehouse(container)
    assert NEW_ITEM not in container.catalog.items
    clock.advance(timedelta(minutes=container.settings.scheduler_catalog_refresh_interval_minutes))
    assert "catalog-refresh" in await container.scheduler.tick()
    await container.scheduler.wait_idle()
    assert NEW_ITEM in container.catalog.items
