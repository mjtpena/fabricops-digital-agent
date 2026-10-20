"""Catalog refresh: reload the estate, diff it against the live catalog, apply the diff and record it.

The catalog is loaded once at start-up; without a refresh, workspaces and items created afterwards stay invisible to
collectors, tools and the dependency graph until the next restart. A refresh builds a complete new snapshot first
(any Fabric error aborts it with nothing applied), refuses obviously broken snapshots, and only then swaps it in.
"""

from __future__ import annotations

from orqentis.config.clock import Clock
from orqentis.contracts.model import EstateContracts
from orqentis.domain.incidents import IncidentStatus, TimelineKind
from orqentis.domain.principals import SYSTEM_PRINCIPAL
from orqentis.fabric.ports import FabricClient
from orqentis.incidents.service import IncidentService
from orqentis.observability.audit import AuditTrail
from orqentis.observability.logging import get_logger
from orqentis.storage.incidents import IncidentQuery
from orqentis.topology.catalog import CatalogDiff, EstateCatalog, diff_entities
from orqentis.topology.graph import DependencyGraph, DependencyStore

_log = get_logger(__name__)


class CatalogRefreshRefusedError(Exception):
    """The reloaded snapshot looks broken (for example an empty estate), so the live catalog was kept."""


class CatalogRefresher:
    def __init__(
        self,
        fabric: FabricClient,
        contracts: EstateContracts,
        catalog: EstateCatalog,
        graph: DependencyGraph,
        dependency_store: DependencyStore,
        incidents: IncidentService,
        audit: AuditTrail,
        clock: Clock,
    ) -> None:
        self._fabric = fabric
        self._contracts = contracts
        self._catalog = catalog
        self._graph = graph
        self._store = dependency_store
        self._incidents = incidents
        self._audit = audit
        self._clock = clock

    async def refresh(self) -> CatalogDiff:
        """Reload workspaces, items, capacities and declared dependencies; apply and report what changed."""
        fresh = await EstateCatalog.load(self._fabric, self._contracts)
        edges = await self._fabric.dependencies.list_dependencies()
        if self._catalog.workspaces and not fresh.workspaces:
            # Fabric answering "no workspaces" (a permission glitch, a bad token) must not silently blind monitoring.
            raise CatalogRefreshRefusedError(
                f"refresh returned no workspaces but the live catalog has {len(self._catalog.workspaces)}; kept it"
            )

        diff = self._catalog.diff(fresh)
        diff.dependencies = diff_entities({e.id: e for e in self._graph.edges}, {e.id: e for e in edges})
        if diff.is_empty:
            _log.info("catalog refresh: no changes")
            return diff

        await self._store.replace_all(edges)
        # From here on there is no await: the catalog and the graph switch together.
        self._catalog.apply(fresh)
        self._graph.replace_edges(edges)

        _log.info("catalog refreshed", extra={"changes": diff.counts()})
        await self._audit.record(
            actor=SYSTEM_PRINCIPAL.id,
            action="catalog.refresh",
            outcome="CHANGED",
            details={"at": self._clock.now().isoformat(), "diff": diff.summary()},
        )
        await self._annotate_incidents_of_removed_items(diff.items.removed)
        return diff

    async def _annotate_incidents_of_removed_items(self, item_ids: list[str]) -> None:
        """Tell the open incidents of an item that left the estate, once, on their timeline.

        Nothing else changes: whether to close such an incident is a human decision. The automatic investigation
        skips them (it cannot resolve the item any more) instead of failing repeatedly.
        """
        open_statuses = [s for s in IncidentStatus if s.is_open]
        for item_id in item_ids:
            try:
                for incident in await self._incidents.repository.list(
                    IncidentQuery(primary_item_id=item_id, statuses=open_statuses)
                ):
                    self._incidents.add_timeline(
                        incident,
                        TimelineKind.NOTE,
                        f"Item {item_id} was removed from the estate (seen by the catalog refresh); "
                        "automatic investigation is skipped for this incident",
                        actor=SYSTEM_PRINCIPAL.id,
                    )
                    await self._incidents.save(incident)
            except Exception:
                _log.warning(
                    "could not annotate incidents of a removed item", extra={"item_id": item_id}, exc_info=True
                )
