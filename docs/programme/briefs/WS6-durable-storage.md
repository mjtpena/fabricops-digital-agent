You are implementing **WS6: Durable storage and stateless app** in `mjtpena/fabricops-digital-agent`.
Base: latest `origin/main`. Branch: `prod/ws6-durable-storage`. Open one PR to `main` when done; never push to `main`.

## Context

Decision D1 / [ADR-0008](../../adr/0008-production-storage.md): PostgreSQL Flexible Server (Entra-only auth through
managed identity) is the system of record; evidence and reports go to Blob/ADLS Gen2 with version-level immutability;
Eventhouse is an optional sink only. Today every deployed store is SQLite or JSONL on an Azure Files SMB share, so the
app is pinned to one replica and a deploy's overlapping revisions are two writers (ledger risk R8).

Wave 1 left these ports ready for you (read them first): `EventStore` (events with a monotonically increasing sequence,
per-collector and per-scope watermarks, a detection cursor whose contract requires a **gap-free committed prefix**:
see the docstring in `src/orqentis/storage/event_store.py` and ADR-0013), `LeaderLease` (holder, TTL, fencing
generation; `src/orqentis/runtime/lease.py`), `ApprovalProvider` with **compare-and-set** transitions (ADR-0015 and the
approval-boundary commit), `IncidentRepository`, `DependencyStore`, `EvidenceStore`, `AuditTrail`
(`observability/audit.py`; the local log re-reads its tail and refuses appends after a shrink). The investigation job
runner (`runtime/investigations.py`) keeps jobs in memory.

## Goal

The app is stateless and safe at two or more replicas and across deploys: every store has a PostgreSQL or Blob adapter
behind the existing ports, one valid audit chain is produced by any number of writers, an approval executes at most
once, scheduled work runs only on the lease holder, data is backed up with point-in-time restore, and local
development, tests and `make demo` keep the SQLite/filesystem/JSONL adapters offline.

## In scope

1. **PostgreSQL adapters** for `EventStore` (including watermarks and the detection cursor: satisfy the gap-free
   committed-prefix contract, e.g. `pg_advisory_xact_lock` around appends, and document the choice),
   `IncidentRepository`, `DependencyStore`, `ApprovalProvider` (compare-and-set with `UPDATE ... WHERE revision = $n`),
   `AuditTrail` (records plus a chain-head row; append under `SELECT ... FOR UPDATE` on the head in the same transaction
   as the insert), `LeaderLease` (lease row: holder, expiry from the **database clock**, fencing generation incremented on
   every acquisition; renewals and fenced writes check the generation).
2. **Durable investigation jobs.** With several replicas, `POST /incidents/{id}/investigate` can land on any replica.
   Persist jobs in PostgreSQL (idempotent per incident via a unique constraint on active jobs); the lease holder (or a
   claim with `FOR UPDATE SKIP LOCKED`) runs them; status reads work from any replica. Keep the in-memory runner for local.
3. **Async driver and Entra auth.** Use one async PostgreSQL driver (psycopg 3 or asyncpg) in an adapter package only.
   Authenticate with an Entra access token for `https://ossrdbms-aad.database.windows.net/.default` from the existing
   credential builder (managed identity in Azure; honour the WS2 authority/sovereign settings), refreshed before expiry
   for pooled connections. No passwords anywhere.
4. **Migrations.** Versioned migrations (Alembic or plain numbered SQL with a version table), run at startup behind an
   advisory lock (safe with several replicas) or as a separate command; document which.
5. **Evidence and reports in Blob** through one adapter speaking the ADLS Gen2 / Blob API (`azure-storage-blob` or
   `azure-storage-file-datalake`, Entra auth), write-once semantics, version-level immutability configured in Bicep, able
   to target a OneLake URL by configuration. Periodically **anchor the audit chain head** (sequence + hash) to an
   immutable blob, and verify against the latest anchor.
6. **Bicep.** PostgreSQL Flexible Server (Burstable `B1ms` default; SKU, storage, HA and backup retention parameters;
   `passwordAuth` disabled, Entra admin set to a parameterised group or principal; the app identity created as a DB role
   by a documented bootstrap step; public access with firewall rules for dev, private endpoint/VNet option), a storage
   account for evidence (Entra-only, shared key disabled, versioning + version-level immutability, soft delete), role
   assignments for the reader identity (Storage Blob Data Contributor scoped to the evidence container only; no
   account-level keys). **Remove the Azure Files mount and storage key from deployed environments**; SQLite stays for
   local and mock. `minReplicas`/`maxReplicas` parameters with defaults of 2 for `test`/`prod`, 1 for `dev`.
7. **Backups and restore.** PITR retention parameter; a restore runbook (`docs/operations/restore.md` or a section in
   `infra/bicep/README.md`) with exact commands to restore to a new server and point the app at it, plus what happens to
   evidence and the audit anchor.
8. Docs: ADR for the implementation choices (driver, migrations, chain serialisation, lease clock, job queue), and
   updates to `docs/architecture.md` (ports table), `docs/security.md` (storage identities, encryption, network),
   `infra/bicep/README.md` (resources, bootstrap, limits section rewritten), `.env.example`, `settings.py`.

## Out of scope

Deploying anything to Azure (the orchestrator does it after owner approval). Eventhouse sinks. New detection or
collectors (WS7). Notifications or UI (WS8). Alerts (WS9). The write executor (WS11).

## Acceptance criteria

1. **Port contract tests** run the same suite against the local adapters and the PostgreSQL/Blob adapters. CI runs them
   with a PostgreSQL service container (add it to `.github/workflows/ci.yml`; keep job names stable or update the
   required-checks list in `docs/delivery.md`) and Azurite for Blob. Locally they skip cleanly without Docker.
2. **Concurrency test:** two app instances (two processes or two containers) against one PostgreSQL produce one valid
   audit chain under interleaved load, never execute one approval twice (N concurrent executes across both → exactly
   one write), and only one runs scheduled jobs at a time (lease handover with fencing).
3. **Detection cursor test:** concurrent appenders with out-of-order commits never cause the cursor to skip an event.
4. **Restart/deploy test:** stopping one instance mid-job and starting another loses no events, jobs or audit records.
5. `make check`, `make demo`, `make demo-all` (offline, local adapters), `pytest -m security`, `docker build`,
   `az bicep build` + `build-params` for every parameter file all pass.
6. No Azure Files mount or storage account key in deployed configurations; no password or connection secret anywhere.

This PR gets an extra independent adversarial review focused on the audit chain and concurrency.
