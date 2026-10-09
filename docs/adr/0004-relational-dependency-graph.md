# ADR-0004: Relational dependency graph; declared lineage

* Status: Accepted (Milestone 3)
* Date: 2026-10-09

## Context

Blast-radius and root-cause analysis need upstream and downstream traversal across pipelines,
notebooks, lakehouses, warehouses, semantic models, reports, data products and external sources.
The specification says not to introduce a graph database unless required.

## Decision

* Edges (`Dependency`) are stored in a relational table. `DependencyGraph` loads them into
  adjacency lists and implements `get_upstream`, `get_downstream`, `get_blast_radius` and
  `find_path` with cycle-safe breadth-first search.
* Each edge type has a defined flow direction (`TRIGGERS`, `WRITES_TO`, `REFRESHES` and
  `PRODUCES` point downstream; `READS_FROM`, `DEPENDS_ON` and `CONSUMES` point upstream).
* Lineage is **declared** in version-controlled contracts (together with data products and
  SLAs), because Fabric exposes no general lineage REST API. Scanner/admin lineage can feed the
  same `DependencyService` port later.
* Blast radius starts from the *failure point* (the target of the failed activity), not the whole
  pipeline. Targets of activities skipped because of the failure count as direct impact.

## Consequences

* Estates of thousands of items traverse in memory comfortably. Very large estates may later need
  incremental loading. Neither case needs a graph database.
