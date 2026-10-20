"""Draft a contracts file from what a live Fabric estate shows, for a human to review.

The scaffold only *reads*, through the same :class:`~orqentis.fabric.ports.FabricClient` port the application
uses (and, optionally, a :class:`~orqentis.contracts.lineage.LineageScanner`):

* ``environments``: one entry per workspace, pre-filled from naming conventions and marked ``inferred``.
* ``dependencies``: ``TRIGGERS`` (and ``REFRESHES``) edges observed in recent pipeline activity runs, from the
  item each activity acts on (``ActivityRun.target_item_id``). With a scanner: report -> semantic model,
  semantic model -> upstream semantic model and semantic model -> data source edges.
* ``external_sources``, ``data_products``, ``slas``: placeholders, marked ``metadata.draft``, for a human to
  complete. Fabric cannot tell us who owns a table or when it must be fresh, so nothing is guessed silently.

The output is a draft. It is never applied automatically; ``contracts validate`` warns about every
unreviewed entry until a human has edited it.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import time

from orqentis.contracts.catalog import CatalogSnapshot
from orqentis.contracts.lineage import LineageScan, LineageScanner
from orqentis.contracts.model import EstateContracts
from orqentis.domain.common import Environment, ItemType
from orqentis.domain.estate import DataProduct, EnvironmentDeclaration, ExternalSource, FabricItem, Workspace
from orqentis.domain.sla import SLA
from orqentis.domain.topology import Dependency, DependencyType
from orqentis.fabric.errors import FabricError
from orqentis.fabric.ports import FabricClient
from orqentis.observability.redaction import redact_text
from orqentis.tools.untrusted import sanitize_text
from orqentis.topology.environment import infer_environment

TODO_OWNER = "TODO: assign an owner"
_OBSERVED = "observed"
_DATA_STORES = frozenset({ItemType.LAKEHOUSE, ItemType.WAREHOUSE})
_ANALYTICAL = _DATA_STORES  # items that hold data worth declaring as a data product
_MAX_NOTE = 120


@dataclass(frozen=True)
class ScaffoldOptions:
    tenant_id: str
    timezone: str = "Australia/Sydney"
    workspaces: tuple[str, ...] = ()  # only these workspaces (id or exact display name); empty means all
    max_runs_per_pipeline: int = 5
    deadline: time = time(6, 0)
    schema_ref: str | None = None


@dataclass
class ScaffoldResult:
    contracts: EstateContracts
    catalog: CatalogSnapshot
    notes: list[str] = field(default_factory=list)  # what was skipped or could not be read, for the operator
    stats: dict[str, int] = field(default_factory=dict)

    def contracts_json(self) -> str:
        document = self.contracts.model_dump(mode="json", by_alias=True, exclude_defaults=True)
        return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def _clean(text: str, limit: int = _MAX_NOTE) -> str:
    return sanitize_text(redact_text(text), max_length=limit)


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:32].strip("-")
    return slug or "workspace"


def _digest(*parts: str, length: int = 8) -> str:
    return hashlib.sha1("|".join(parts).encode(), usedforsecurity=False).hexdigest()[:length]


def _reason(exc: FabricError) -> str:
    """Why a read failed, without the service's free text (which is untrusted)."""
    return exc.error_code or (f"HTTP {exc.status_code}" if exc.status_code else type(exc).__name__)


def _edge_type(target: FabricItem) -> DependencyType | None:
    """How a pipeline relates to the item an activity acts on; None when the direction cannot be known."""
    if target.item_type is ItemType.SEMANTIC_MODEL:
        return DependencyType.REFRESHES
    if target.item_type in _DATA_STORES:
        return None  # a copy activity may read or write a lakehouse: that needs a human
    return DependencyType.TRIGGERS


def _dependency(source: str, target: str, kind: DependencyType, tenant_id: str, **metadata: object) -> Dependency:
    return Dependency(
        id=f"dep-{kind.value.lower().replace('_', '-')}-{_digest(source, target, kind.value, length=12)}",
        tenant_id=tenant_id,
        source_item_id=source,
        target_item_id=target,
        dependency_type=kind,
        source=_OBSERVED,
        metadata=dict(metadata),
    )


async def scaffold_contracts(
    fabric: FabricClient, options: ScaffoldOptions, *, scanner: LineageScanner | None = None
) -> ScaffoldResult:
    """Read the estate and return a draft. Makes no write call; ``scanner`` adds lineage when given."""
    notes: list[str] = []
    all_workspaces = sorted(await fabric.workspaces.list_workspaces(), key=lambda w: (w.display_name, w.id))
    wanted = set(options.workspaces)
    workspaces = [w for w in all_workspaces if not wanted or w.id in wanted or w.display_name in wanted]
    if wanted and not workspaces:
        notes.append("no workspace matched --workspace; nothing was scaffolded")

    items: list[FabricItem] = []
    items_by_workspace: dict[str, list[FabricItem]] = {}
    for workspace in workspaces:
        try:
            listed = sorted(await fabric.items.list_items(workspace.id), key=lambda i: (i.display_name, i.id))
        except FabricError as exc:
            notes.append(f"workspace {workspace.id}: items not listed ({_reason(exc)}); grant the reader Viewer access")
            listed = []
        items_by_workspace[workspace.id] = listed
        items.extend(listed)
    catalog = CatalogSnapshot.of(workspaces, items)
    inferred = {w.id: infer_environment(w.display_name) for w in workspaces}

    edges: dict[tuple[str, str, DependencyType], Dependency] = {}
    skipped_data_store_targets = 0
    activity_counts: dict[str, int] = defaultdict(int)
    for workspace in workspaces:
        for pipeline in (i for i in items_by_workspace[workspace.id] if i.item_type is ItemType.DATA_PIPELINE):
            seen: dict[str, set[str]] = defaultdict(set)  # target -> activity names
            runs_seen: dict[str, set[str]] = defaultdict(set)  # target -> run ids
            try:
                runs = await fabric.jobs.list_job_instances(
                    workspace.id, pipeline.id, limit=options.max_runs_per_pipeline
                )
                for run in runs:
                    for activity in await fabric.pipelines.query_activity_runs(workspace.id, pipeline.id, run.id):
                        activity_counts["activity_runs_read"] += 1
                        if activity.target_item_id and activity.target_item_id != pipeline.id:
                            seen[activity.target_item_id].add(_clean(activity.activity_name, 60))
                            runs_seen[activity.target_item_id].add(run.id)
            except FabricError as exc:
                notes.append(f"pipeline {pipeline.id}: run history not read ({_reason(exc)})")
            for target_id, names in sorted(seen.items()):
                target = catalog.items.get(target_id)
                if target is None:
                    notes.append(
                        f"pipeline {pipeline.id}: activity target {target_id} is not in the scanned workspaces"
                    )
                    continue
                kind = _edge_type(target)
                if kind is None:
                    skipped_data_store_targets += 1
                    continue
                edge = _dependency(
                    pipeline.id,
                    target_id,
                    kind,
                    options.tenant_id,
                    activities=sorted(names)[:5],
                    runs_observed=len(runs_seen[target_id]),
                )
                edges.setdefault((edge.source_item_id, edge.target_item_id, kind), edge)
    if skipped_data_store_targets:
        notes.append(
            f"{skipped_data_store_targets} pipeline activity target(s) are lakehouses or warehouses: whether the "
            "pipeline reads or writes them cannot be observed, so add READS_FROM / WRITES_TO edges by hand"
        )

    external_sources: list[ExternalSource] = []
    if scanner is not None:
        external_sources, scanned_edges, scan_notes = await _lineage(
            scanner, workspaces, catalog, inferred, options.tenant_id
        )
        notes.extend(scan_notes)
        for edge in scanned_edges:
            edges.setdefault((edge.source_item_id, edge.target_item_id, edge.dependency_type), edge)

    environments = [
        EnvironmentDeclaration(
            workspace_id=w.id,
            environment=inferred[w.id],
            inferred=True,
            note=_clean(f"drafted from the workspace name {w.display_name!r}", 200),
        )
        for w in workspaces
    ]
    unknown = [w.id for w in workspaces if inferred[w.id] is Environment.UNKNOWN]
    if unknown:
        notes.append(
            f"{len(unknown)} workspace(s) have no environment in their name; their entries say UNKNOWN: "
            "declare DEV, TEST or PROD"
        )

    if not external_sources:
        external_sources = _placeholder_source(workspaces, items_by_workspace, inferred, options)
    data_products, slas = _placeholder_products(workspaces, items_by_workspace, inferred, edges, options)

    contracts = EstateContracts(
        schema_ref=options.schema_ref,
        environments=environments,
        external_sources=external_sources,
        data_products=data_products,
        dependencies=sorted(
            edges.values(), key=lambda e: (e.source_item_id, e.target_item_id, e.dependency_type.value)
        ),
        slas=slas,
    )
    stats = {
        "workspaces": len(workspaces),
        "items": len(items),
        "environments": len(environments),
        "dependencies": len(contracts.dependencies),
        "external_sources": len(external_sources),
        "data_products": len(data_products),
        "slas": len(slas),
        **activity_counts,
    }
    return ScaffoldResult(contracts, catalog, notes, stats)


def _placeholder_source(
    workspaces: list[Workspace],
    items_by_workspace: dict[str, list[FabricItem]],
    inferred: dict[str, Environment],
    options: ScaffoldOptions,
) -> list[ExternalSource]:
    """One example source in the first workspace that has a pipeline: Fabric cannot discover what feeds it."""
    for workspace in workspaces:
        if any(i.item_type is ItemType.DATA_PIPELINE for i in items_by_workspace[workspace.id]):
            return [
                ExternalSource(
                    id=f"ext-todo-{_digest(workspace.id)}",
                    tenant_id=options.tenant_id,
                    workspace_id=workspace.id,
                    display_name="TODO: name an upstream system (then add READS_FROM edges from its pipelines)",
                    environment=inferred[workspace.id],
                    owner=TODO_OWNER,
                    metadata={"draft": True},
                )
            ]
    return []


def _placeholder_products(
    workspaces: list[Workspace],
    items_by_workspace: dict[str, list[FabricItem]],
    inferred: dict[str, Environment],
    edges: dict[tuple[str, str, DependencyType], Dependency],
    options: ScaffoldOptions,
) -> tuple[list[DataProduct], list[SLA]]:
    """A draft data product per workspace that holds lakehouses or warehouses, and a draft SLA for it.

    The freshness item is the pipeline in the workspace with the most observed outgoing edges. The deadline is
    a plain default that a human must replace.
    """
    out_degree: dict[str, int] = defaultdict(int)
    for source, _target, _kind in edges:
        out_degree[source] += 1
    products: list[DataProduct] = []
    slas: list[SLA] = []
    for workspace in workspaces:
        stores = [i for i in items_by_workspace[workspace.id] if i.item_type in _ANALYTICAL]
        if not stores:
            continue
        product_id = f"dp-{_slug(workspace.display_name)}-{_digest(workspace.id, length=6)}"
        name = _clean(workspace.display_name, 80)
        products.append(
            DataProduct(
                id=product_id,
                tenant_id=options.tenant_id,
                workspace_id=workspace.id,
                display_name=f"TODO: name the data product in {name}",
                owner=TODO_OWNER,
                environment=inferred[workspace.id],
                backing_item_ids=[i.id for i in stores][:20],
                metadata={"draft": True},
            )
        )
        pipelines = [i for i in items_by_workspace[workspace.id] if i.item_type is ItemType.DATA_PIPELINE]
        if not pipelines:
            continue
        freshness = max(pipelines, key=lambda p: (out_degree[p.id], p.display_name, p.id))
        slas.append(
            SLA(
                id=f"sla-{product_id.removeprefix('dp-')}",
                tenant_id=options.tenant_id,
                data_product_id=product_id,
                display_name=f"TODO: availability of the data product in {name}",
                description="TODO: state the real deadline. The time below is a placeholder.",
                deadline_local_time=options.deadline,
                timezone=options.timezone,
                freshness_item_id=freshness.id,
                owner=TODO_OWNER,
                metadata={"draft": True},
            )
        )
    return products, slas


async def _lineage(
    scanner: LineageScanner,
    workspaces: list[Workspace],
    catalog: CatalogSnapshot,
    inferred: dict[str, Environment],
    tenant_id: str,
) -> tuple[list[ExternalSource], list[Dependency], list[str]]:
    notes: list[str] = []
    try:
        scan: LineageScan = await scanner.scan([w.id for w in workspaces])
    except FabricError as exc:
        return [], [], [f"scanner lineage not added ({_reason(exc)}); the sign-in must be a Fabric administrator"]

    datasources = {s.id: s for s in scan.datasources}
    sources: dict[str, ExternalSource] = {}
    edges: list[Dependency] = []

    def known(item_id: str) -> bool:
        return item_id in catalog.items

    for dataset in sorted(scan.datasets, key=lambda d: d.id):
        if not known(dataset.id):
            continue
        for upstream in dataset.upstream_dataset_ids:
            if known(upstream) and upstream != dataset.id:
                edges.append(_dependency(dataset.id, upstream, DependencyType.READS_FROM, tenant_id, via="scanner"))
        for datasource_id in dataset.datasource_ids:
            datasource = datasources.get(datasource_id)
            if datasource is None:
                continue
            source_id = f"ext-{_digest(datasource_id, length=12)}"
            if source_id not in sources:
                sources[source_id] = ExternalSource(
                    id=source_id,
                    tenant_id=tenant_id,
                    workspace_id=dataset.workspace_id,
                    display_name=_clean(f"{datasource.kind}: {datasource.detail}".strip(": "), 160),
                    environment=inferred.get(dataset.workspace_id, Environment.UNKNOWN),
                    owner=TODO_OWNER,
                    metadata={"draft": True, "datasource_kind": datasource.kind, "datasource_id": datasource.id},
                )
            edges.append(_dependency(dataset.id, source_id, DependencyType.READS_FROM, tenant_id, via="scanner"))
    for report in sorted(scan.reports, key=lambda r: r.id):
        if report.dataset_id and known(report.id) and known(report.dataset_id):
            edges.append(_dependency(report.id, report.dataset_id, DependencyType.DEPENDS_ON, tenant_id, via="scanner"))
    if not scan.datasets and not scan.reports:
        notes.append("the scanner returned no semantic models or reports for the selected workspaces")
    return list(sources.values()), edges, notes
