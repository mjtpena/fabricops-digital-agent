# ADR-0017: PostgreSQL system of record, Blob evidence and an application that is safe at two or more replicas

* Status: Accepted (production programme, WS6; implements [ADR-0008](0008-production-storage.md))
* Date: 2026-10-20

## Context

ADR-0008 decided *where* production state lives: PostgreSQL Flexible Server (Entra-only) for incidents, approvals,
dependencies, events, leases and the audit chain, and immutable Blob storage for evidence. Until this change every
store was a SQLite file, a JSONL file or an in-process object on one Azure Files share, so the app was pinned to one
replica and a deploy's overlapping revisions were two writers (ledger risk R8). This ADR records *how* the storage
ports are implemented on PostgreSQL and Blob and which guarantees each adapter gives, because several of them are
safety properties that other ADRs already depend on:

* the detection cursor needs `EventStore.read_after` to return a **gap-free committed prefix** (ADR-0013 section 7);
* approvals are decided and executed with **compare-and-set** (ADR-0015 section 5);
* scheduled work runs only on the **lease holder**, and a deposed leader's late write must be refused (ADR-0013
  section 2: "nothing fences yet");
* the audit trail is **one hash chain**, however many processes append to it (invariant 7: execution re-checks
  everything, and the evidence of it must be tamper-evident).

## Decision

### 1. One async driver, in two adapter packages

* `orqentis.storage.postgres` (asyncpg) and `orqentis.storage.blob` (`azure-storage-blob`, aio) are the only modules
  that import those SDKs, besides `bootstrap.py`. A test walks the source tree and fails if any other module does.
* **asyncpg rather than psycopg 3.** Both satisfy the brief. asyncpg runs on the Windows default event loop (psycopg's
  async mode refuses it, which would make the local test loop depend on a policy hack), needs no `libpq` in the image
  (the Azure Linux base image has none, and a bundled one would be another thing to patch), and takes `password` as a
  callable that it awaits for every new connection, which is exactly the shape of Entra token authentication.
* `local` remains the default storage backend and keeps SQLite, filesystem evidence, the JSONL audit log and the
  in-process lease and job store; mock mode and `make demo` are unchanged and offline. `ORQENTIS_STORAGE_BACKEND=postgres`
  selects the new adapters. `dev`, `test` and `prod` **require** it (`Settings.check_storage_for_environment`, enforced
  by `orqentis serve` and by the container build): two replicas, or one that restarts, on SQLite files would each
  hold a different truth.

### 2. Authentication: an Entra token is the password; there is no password setting

* The password of a **new** connection is an access token for `https://ossrdbms-aad.database.windows.net/.default`
  (`ORQENTIS_POSTGRES_SCOPE` for other clouds) from the existing token provider. It follows
  `ORQENTIS_LLM_AUTH_MODE` when set, because PostgreSQL and Blob are resources of the deployment (the reader managed
  identity), not of the Fabric tenant a cross-tenant service principal reaches; the authority host (WS2) applies.
* The provider caches a token until five minutes before it expires, and asyncpg asks for the password only when it
  opens a connection, so no connection is ever opened with a token about to lapse. Established connections stay valid
  after their token expires (the token is checked at login only); the pool closes idle connections after five
  minutes, so a long-lived replica keeps re-authenticating. A connection that cannot be opened is attempted three times
  with backoff (a failover, a role that was created seconds ago).
* TLS is `verify-full` by default (`ORQENTIS_POSTGRES_SSL_ROOT_CERT` adds a CA bundle). `auth=none` and `ssl=disable`
  exist for a throwaway local container with trust authentication and are **rejected outside `local`**.
* The database role is the managed identity's name. `scripts/postgres-bootstrap.sql` (run once by the Entra
  administrator) creates the role, a schema it **owns**, and `CONNECT`; the application then creates and migrates its own
  tables. It has no superuser, no `CREATE` on the database and nothing in other schemas. Verified: the application runs
  its migrations and appends audit records as such a role.

### 3. Migrations: numbered SQL, a version table, an advisory lock, forward only

* `migrations/NNNN_name.sql` are applied in order, each in a transaction with its row in `schema_migrations`
  (version, name, SHA-256). A recorded checksum that no longer matches the file stops the start-up ("a released
  migration must never be edited").
* **At start-up, behind a session-level advisory lock** (`ORQENTIS_POSTGRES_MIGRATE_ON_START`, default true): replicas
  that start together wait for the one that holds the lock and then find nothing to do. Alternative: set it to false and
  run `orqentis db migrate` as a separate step; the app then refuses to start while migrations are pending.
  Migrations run **as the schema owner**; `orqentis db migrate` refuses any other role.
* A schema that is *ahead* of the code (a newer revision migrated while the old one still serves) is tolerated, so every
  migration must stay compatible with the previous release: add before you use, remove only in a later release.
  There are no down migrations; the way back is a point-in-time restore (`docs/operations/restore.md`).

### 4. Event store: appends are serialised, so `read_after` is a committed prefix

A `bigserial`/identity value is assigned at insert time, not commit time; two concurrent appenders can commit in the
opposite order and a consumer that has read past the higher number skips the lower one forever (demonstrated by a
control test with a bare identity column). **Every append takes `pg_advisory_xact_lock` before its insert.** Sequence
numbers are therefore assigned in lock order, and the lock is released only after the transaction has committed and
become visible (PostgreSQL removes the transaction from the in-progress set before it releases its locks), so a reader
that can see `n + 1` can see `n`. A rolled-back transaction leaves a hole in the numbering, which is harmless: the
number was never visible. Readers take no lock. Inserts are `INSERT ... SELECT ... FROM unnest(...) ORDER BY ordinal
ON CONFLICT DO NOTHING`, one statement per batch, with duplicates (by `event_id` or `dedupe_key`) skipped in place.

*Alternative considered:* let appenders run in parallel and have readers stop below the oldest in-flight
transaction (`pg_snapshot_xmin`). It scales further, but the reader's correctness then depends on every writer
recording its transaction id and on no long transaction anywhere in the database, which would stall detection.
Event appends are small batches every few minutes; serialising them costs nothing and keeps the argument one line long.

Bodies are stored as **text** (the exact JSON the domain model produced), not `jsonb`: `jsonb` rejects a NUL
character inside a string (which untrusted Fabric text can contain), and re-serialising changes number formatting,
which would break the audit hashes. Indexed columns carry what is queried.

### 5. Approvals: `UPDATE ... WHERE revision = $n`

`transition` is one conditional update; PostgreSQL serialises racing updates of a row, the loser re-evaluates its
condition against the new row and updates nothing. Decisions, expiry, cancellation and the execution claim therefore
have exactly one winner across any number of processes, and a write behind the claim happens at most once.

### 6. Audit trail: a head row, `SELECT ... FOR UPDATE`, anchors in immutable storage

* `audit_chain_head` is one row. An append locks it (`FOR UPDATE`), reads the last sequence and hash, inserts the
  record and moves the head, in one transaction: no two records can share a predecessor, so the chain cannot fork
  whatever the interleaving. A failed append consumes no sequence number. A writer that dies holding the lock loses its
  connection and PostgreSQL rolls it back (tested by dropping the connection). `audit_records` also refuses `UPDATE`,
  `DELETE` and `TRUNCATE` with triggers: defence in depth against bugs, **not** the tamper evidence (a privileged
  user can drop a trigger).
* **Verification** re-derives every hash from the stored body, checks contiguity, that the last record is the head,
  and the latest **anchor**. Records appended while it runs are beyond the head it started from.
* **Anchors.** Every 15 minutes (leader-only job) the head `(epoch, sequence, hash)` is written to a blob with
  `If-None-Match: *` in a container with version-level immutability. Whoever rewrites the table, recomputes every hash
  and moves the head, or deletes the tail, contradicts an anchor the database owner cannot change: verified against
  all three attacks (`tests/concurrency/test_audit_chain.py`). Two different hashes anchored for one sequence are
  reported as a conflict.
* **Epochs.** After a restore the database may be *behind* its latest anchor; that is reported (records are lost) and
  stays reported until an operator runs `orqentis audit start-epoch --reason ...`, which appends an `audit.epoch`
  record naming the head and the last anchor of the epoch that ends, and begins a new anchor epoch. Old anchors stay
  as the evidence of the loss. Without this, a legitimate restore would leave verification failing forever.
* The timestamp is the application clock's, taken while holding the head lock. Audit appends are never fenced (see 7):
  a record of something that happened is kept even from a deposed leader.

### 7. Leases with the database clock; fenced writes

* `leases(name, holder_id, generation, expires_at)`. Acquire and renew compare `expires_at` with the **database's**
  `now()` in the statement that changes it, so replicas with skewed or paused clocks cannot both believe they lead.
  `generation` (the fencing token) is incremented on every new tenure (free, expired or released) and never reset; a
  released lease keeps its row. `renew` fails only when the lease was released or taken over, not merely because it
  expired, which is the port's contract. The `Lease` returned carries `expires_at` on the *application* clock (the
  instant the request was sent plus the remaining time the database reports), which can only be early.
* **Fencing.** The scheduler runs each job under a `Fence(lease, holder, generation)` held in a `ContextVar`. Every write
  transaction of the PostgreSQL adapters first runs `SELECT ... FROM leases WHERE name AND holder_id AND generation
  FOR SHARE` and raises `FencingError` if no row matches. The share lock stays for the whole transaction, so a competing
  `acquire` (an `UPDATE` of that row) **waits for in-flight fenced writes**, and every later fenced write sees the new
  generation: the check and the write are one atomic unit, with no window for a paused leader to slip through
  (`tests/concurrency/test_lease_fencing.py`). Writes outside a job (API requests, audit records) carry no fence. The
  durable investigation workers start in a clean context.
* **Mutex for check-then-act.** Detection looks for an incident covering a failure and opens one if there is none. The
  leader's scheduled run and a `POST /telemetry/collect` that lands on another replica could both find none. Detection
  passes therefore hold a named mutex (`pg_try_advisory_lock` on a dedicated connection, bounded wait, released by the
  server if the instance dies); `InProcessMutex` serves local storage.

### 8. Durable investigation jobs

* `investigation_jobs` with a **partial unique index** on `incident_id WHERE status IN ('QUEUED','RUNNING')`:
  submission is idempotent per incident across replicas (`ON CONFLICT DO NOTHING`, then return the active job).
* **Every replica runs a worker loop** (poll every 2 s) that claims with `FOR UPDATE SKIP LOCKED`, bounded by
  `ORQENTIS_INVESTIGATION_MAX_CONCURRENCY` per replica; the scheduler's auto-investigation only *submits*. A claim
  increments `claim_seq`; `heartbeat`, `finish` and `release` must present worker and claim number, so a worker that
  stalled and was replaced cannot overwrite the new worker's job. Status reads work from any replica.
* **Crashes.** A claim not renewed within the TTL (120 s; heartbeat every third) is released by the next worker poll:
  the job is queued again with `attempts + 1`; at 3 abandoned claims it fails instead ("a job that kills its worker
  cannot loop forever"), and its incident goes back to `DETECTED`. A re-claimed job first resets its incident, still
  `INVESTIGATING` from the dead worker. A **graceful stop** releases running jobs to the queue immediately (no retry
  is used up). `AutoInvestigationTask` now judges "orphaned" by the job store, so a replica never mistakes another
  replica's running investigation for an orphan (ADR-0013 consequence).
* The in-memory store keeps the previous behaviour for local runs (same runner, `shared = False`, no polling).
* The runner's query methods are now `async` (they read a shared store).

### 9. Incident numbers come from a counter row

`INC-<year>-<n>` was "max stored + 1", which two replicas can compute at the same time and then overwrite each other's
incident with an upsert. A per-year counter row, seeded from the highest stored number, is incremented in the same
transaction that returns it. After a restore the counters can be behind the evidence container (evidence is not part
of the database backup); `orqentis db reseed-counters` raises them above every incident that still has evidence.

### 10. Evidence, reports and anchors in Blob storage

* One adapter over the **Blob API**, Entra credentials only (no account key or SAS exists in the code path), able to
  target **OneLake** by URL (`ORQENTIS_EVIDENCE_BLOB_URL`, the workspace as container, the item folder as
  `ORQENTIS_EVIDENCE_PATH_PREFIX`). The DFS (ADLS Gen2 hierarchical) API is not used: Azurite does not implement it, and
  nothing here needs directories.
* **Write-once** artefacts use `If-None-Match: *` (409 -> `EvidenceExistsError`, as the filesystem adapter does).
  Reports are rewritten when an incident is investigated again; the storage account has blob versioning, so an
  overwrite creates a new version and the previous one stays protected by the version-level immutability policy.
  *Unverified offline:* that a current version under a retention period accepts an overwrite. If it does not, reports
  need a revision suffix; see open questions.
* `docs/operations/restore.md`: what a database restore does to evidence and anchors.

### 11. Infrastructure and scaling

PostgreSQL Flexible Server (B1ms default; `passwordAuth` disabled; Entra administrator group; PITR retention 7-35 days;
public access with firewall rules for dev or VNet integration), an evidence storage account (shared key disabled,
versioning, soft delete, account-level version-level immutability, an `evidence`, an `audit-anchors` and a `contracts`
container), reader-identity role assignments scoped to those containers only (Blob Data Contributor on the first two,
Blob Data **Reader** on `contracts`), and `minReplicas`/`maxReplicas` (1 in dev, 2 in test and prod). **Azure Files, the
storage key and the SMB mount are removed.** The estate contracts file, which lived on the share, becomes a blob
(section 12). See `infra/bicep/README.md`.

### 12. The estate contracts file is a blob the app can only read

* **Where.** `contracts/estate.json` in the evidence account (`ORQENTIS_CONTRACTS_CONTAINER`,
  `ORQENTIS_CONTRACTS_BLOB_NAME`; Bicep `contractsContainerName` / `contractsBlobName`). `ORQENTIS_CONTRACTS_PATH` stays
  for local and mock runs and, when set, wins over the blob. A container secret was rejected: it is copied into every
  revision, is not versioned or immutable, and its value would have to pass through CI as a variable.
* **Who.** The app's identity has Storage Blob Data **Reader** on that container only; it cannot change what it reads.
  A person with Storage Blob Data Contributor on that container alone uploads the reviewed file
  (`az storage blob upload --auth-mode login --overwrite`). The container has versioning, soft delete and the account's
  retention, so every upload stays readable. The environment declarations in this file are a trust decision (a workspace
  declared DEV lowers severity and stops production-only policy rules), hence the narrow, separate, audited write path.
* **Loading.** `orqentis.contracts.source` has two sources (`FileContractsSource`, and `BlobContractsSource` in the Blob
  adapter package) and one loader, so both raise the same typed `ContractsError` naming where the document came from and
  every violation, never echoing values from it. The SHA-256 of the **bytes** is the file's identity.
* **Not ready, not a crash loop.** A missing or invalid blob at start-up fails the container build with the typed error;
  the process stays up, `/health` answers, `/health/ready` is 503 with `build_failed` and the exception *type* only, and
  the build is retried with backoff until the upload fixes it. Nothing restarts.
* **Reload without a restart.** The catalog refresh re-reads the source first. An unchanged SHA-256 is a no-op; a changed
  one is validated, applied in place (REST client, environment resolver, catalog) and audited as `contracts.loaded` with
  the new SHA-256, blob path and version. **An invalid or missing blob on a reload keeps the last good contracts in
  force** and fails the refresh visibly (the scheduled job reports `failed`, the log says why); the app does not go
  not-ready at runtime, because that would turn a bad upload into an outage of a system that is monitoring production.
* **Every replica.** The catalog, the dependency graph and the contracts live in each process's memory, and
  investigations run on whichever replica claims them. The `catalog-refresh` job runs on the leader only, so a follower
  would have kept the start-up estate indefinitely. `ReplicaCatalogSync` refreshes each replica's own memory on the same
  interval (the leader skips it: its job does the same and, once per change, the shared effects: the dependency table,
  the audit of the diff, the incident annotations). A failing run is logged on the change of health and retried.

## Consequences

* The app can run as two or more replicas and deploys can overlap: one audit chain, at most one execution per
  approval, one scheduler leader, queued work that survives a replica.
* **Residual limits (follow-ups):**
  * `IncidentRepository.save` stays a last-writer-wins upsert (as in SQLite). A row version for the aggregate is the fix;
    approvals, audit, leases and jobs, which carry the safety rules, do not depend on it.
  * Rate limits are per replica and in memory (ADR-0015 section 3), so the effective limit is N times the setting.
  * Immutability lasts for the retention period (`evidenceRetentionDays`), after which the reader identity (Blob Data
    Contributor) could delete an old anchor. Verification compares only the *latest* anchor, which is at most one
    interval old and always inside the period, so expiry of old anchors loses forensic history, not protection. Lock the
    policy in production and keep the retention long.
  * Detection serialises on a mutex; a very long detection pass delays other replicas' manual collections up to the
    wait bound (2 minutes) and then fails visibly.
  * Anchoring runs every 15 minutes (leader-only). Records newer than the latest anchor are protected by the hash
    chain alone: someone with full database rights could rewrite that last interval before the next anchor covers it.
    A shorter interval narrows the window (`ORQENTIS_SCHEDULER_AUDIT_ANCHOR_INTERVAL_MINUTES`).
* A PostgreSQL outage stops the app (readiness fails; the build is retried with backoff), by design: the system of
  record is unavailable.
* Costs: B1ms about USD 24/month plus storage; the connection budget is `replicas x ORQENTIS_POSTGRES_POOL_MAX` (+ the
  migration connection) and must stay under the SKU's limit (B1ms: about 50 minus reserved); Bicep checks it.

## Open questions for the orchestrator

1. Does a Blob version under a retention policy accept an overwrite (the `report.*` re-render)? Verify on the first
   deployed environment; the fallback is a revision suffix.
2. Account-level version-level immutability can only be enabled when the account is created, so an environment created
   by the old template needs a new evidence account (it is a new account in the template).
3. Moving rate limiting to a shared store (ADR-0015 follow-up) is not done here. Replacing the contracts blob by an
   overwrite relies on the same Blob behaviour as question 1 (an overwrite of a current version under retention creates a
   new version); if the service refuses it, the fallback is a new `contractsBlobName` per release.
4. `Locked` immutability is irreversible and is the owner's call for `prod`.

## Alternatives considered

* **psycopg 3** (see section 1). **SQLAlchemy/Alembic**: an ORM and a migration framework for 15 tables and one SQL
  file is more machinery than it removes; the tables are accessed with a handful of statements whose locking matters.
* **PgBouncer / Azure built-in pooling**: the app is the only client and keeps small pools; fewer moving parts.
* **Lease in Blob storage (leader election by blob lease)**: a second system whose clock and failure modes differ from
  the database that the fenced writes must be checked against; the fence would not be atomic with the write.
* **A queue service for investigations**: another resource to deploy and secure for a few jobs per hour; the database
  already gives exactly-once claims and durability.
* **Anchoring to an external timestamping service**: stronger against a hostile storage-account owner, but adds a
  third party; the immutable blob already puts the anchor beyond the database owner.
