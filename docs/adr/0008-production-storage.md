# ADR-0008: Production storage: PostgreSQL system of record, immutable Blob evidence

* Status: Accepted (programme decision D1)
* Date: 2026-10-20
* Supersedes: the open question in [ADR-0003](0003-local-stores-first.md) on where incidents and
  approvals live in production. ADR-0003's local stores remain the local and mock-mode adapters.

## Context

The deployed MVP keeps SQLite databases and a JSONL audit log on an Azure Files SMB share mounted
with `nobrl`. That allows exactly one writer: the app is pinned to one replica, and the overlapping
revision during a deploy means two writers for a short time. There are no backups. Production needs
two or more replicas, zero-downtime deploys, an audit chain that stays valid across replicas, and
backups with point-in-time restore.

ADR-0003 named Eventhouse and OneLake as targets. A monitoring tool must not keep its system of
record inside the platform it monitors: when Fabric capacity is throttled, paused or down is exactly
when FabricOps must still record incidents, approvals and audit records.

## Decision

* **Azure Database for PostgreSQL Flexible Server** is the system of record for incidents,
  approvals, dependencies, the operational event store (with collector watermarks), scheduler
  leases and the audit trail (records and chain head).
  * Microsoft Entra authentication only (password authentication disabled); the app connects with
    its managed identity.
  * The audit hash chain is serialised across replicas with row-level locking on a single
    chain-head row (`SELECT ... FOR UPDATE` in the same transaction as the append), so concurrent
    appends from different replicas produce one linear, verifiable chain.
  * Scheduler leases use the same database (lease row with holder, expiry and fencing token).
  * Burstable tier outside `prod`; backups with point-in-time restore everywhere.
* **Evidence and reports** go to Azure Blob Storage (ADLS Gen2 API) with **version-level
  immutability**, behind one adapter speaking the ADLS API, so the same adapter can target OneLake
  by URL where a customer prefers it. The audit chain head is periodically anchored to immutable
  storage.
* **Eventhouse** is an optional analytics sink and signal source, never the system of record.
* Local development, tests and `make demo` keep the SQLite, filesystem and JSONL adapters
  (ADR-0003). Deployed environments use no SQLite and no Azure Files.

## Consequences

* Storage ports gain PostgreSQL and Blob adapters with versioned migrations; port contract tests
  run against both local and cloud adapters (a PostgreSQL service container in CI).
* The app becomes stateless and can run as 2+ replicas with leader election (WS1, WS6).
* New Azure resources per environment (PostgreSQL server, storage account), with private
  networking options and Entra-only auth in Bicep.
* Restore is rehearsed and documented before production sign-off.
