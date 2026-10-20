# ADR-0003: Local development stores first; Eventhouse and OneLake behind the same ports

* Status: Accepted (Milestone 2). The production-storage question is decided by [ADR-0008](0008-production-storage.md)
* Date: 2026-10-09

## Context

The target architecture uses Fabric Eventhouse (KQL) for operational events and OneLake for
evidence. Both need a live tenant, and the MVP demo must run without one.

## Decision

* `EventStore` → `LocalEventStore` (SQLite, deduplicated by `dedupe_key`).
* `EvidenceStore` → `LocalEvidenceStore` (filesystem, write-once raw evidence, path-traversal
  safe, `evidence://` URIs).
* `IncidentRepository` → `SqliteIncidentRepository` (document-style JSON plus indexed columns).
* `AuditTrail` → `LocalAuditLog` (hash-chained JSONL).
* Evidence and reports are kept apart from agent context and outlive any conversation.

`FabricEventhouseStore` is milestone 9. Deliberately, no placeholder implementation is shipped.
The production home of incidents and approvals (Eventhouse tables versus Azure SQL) is left open
until milestone 9.

## Consequences

* A deployed MVP runs as a single replica with an Azure Files volume (see `infra/bicep`).
* Interfaces are async and storage-agnostic, so cloud adapters are additive.
