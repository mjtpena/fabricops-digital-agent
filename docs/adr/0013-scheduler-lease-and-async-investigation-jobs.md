# ADR-0013: In-process scheduler behind a leader lease, asynchronous investigation jobs, durable detection and incremental collection

* Status: Accepted (production programme, WS1; revised after the orchestrator's review of the first version)
* Date: 2026-10-20

## Context

FabricOps did nothing on its own. Collection and detection ran only when someone called
`POST /telemetry/collect`; an investigation ran only inside the HTTP request of
`POST /incidents/{id}/investigate`, so a slow LLM investigation could exceed the Container Apps
ingress timeout (~240 s); the estate catalog was read once at start-up, so new workspaces and items
stayed invisible until a restart; collectors re-read a fixed 7-day window and paged the full job
history of every pipeline on every run; nothing limited how hard Fabric was called; and the
container (including a full read of the estate) was built inside the FastAPI lifespan, before the
port opened, so a large tenant or a bad contracts file could fail the startup probe and cause a
restart loop.

The target is a worker that operates safely on a schedule. The system of record moves to PostgreSQL
later (ADR-0008) and the app then runs as two or more replicas, so the design must not need a
rewrite when that happens.

## Decision

1. **A small in-process scheduler** (`orqentis.runtime.scheduler`) started and stopped in the
   FastAPI lifespan. Jobs are `JobSpec(name, interval, async function)`; each can be enabled or
   disabled and has an interval (settings and Bicep parameters). It is driven by the injected
   `Clock`: `tick()` starts what is due, and the background loop only calls `tick()` every few
   seconds, so tests advance a fake clock and never wait.
2. **A `LeaderLease` port** (`acquire(name, ttl)`, `renew`, `release`; a holder id and a
   monotonic **fencing token**). Only the holder runs jobs; the scheduler renews when a third of the
   TTL is left (settings require TTL >= 3 x tick, so a renewal attempt leaves at least one whole tick before expiry) and cancels running jobs when it loses the lease or
   cannot reach the lease backend. Every job receives a `JobContext` (job name, holder id, fencing
   token, start time) so that a store that supports fenced writes can reject a deposed leader's late
   write; nothing fences yet. We ship `InProcessLeaderLease` now (correct for one process) and a
   database-backed implementation arrives with PostgreSQL (WS6) without touching the scheduler.
   `renew` fails only if the lease was released or taken over, not merely because it expired, so a
   slow leader nobody replaced keeps its tenure. The log and audit key for the token is
   `lease_generation`: redaction blanks any key containing "token".
3. **Jobs never overlap with themselves.** A run that is still going when its next slot arrives
   makes that slot be skipped and counted; slots missed while busy collapse into one run. A job
   exception or timeout is logged (redacted), recorded in the job status, and audited when job
   health *changes* (first failure, recovery), never on every retry. It does not stop the scheduler.
4. **Four jobs**, all idempotent: `collect-detect` (5 min), `auto-investigate` (1 min), `sla-sweep`
   (15 min, first run one interval after start so the first collection precedes it) and
   `catalog-refresh` (60 min, first run one interval after start).
   * The SLA sweep appends one `SLA_ASSESSED` event each time an SLA enters a new state for a
     deadline day (dedupe key `SLA_ASSESSED:<sla>:<deadline date>:<state>`). The payload is the full
     `SLAAssessment`, so a detection rule matches `event_type == SLA_ASSESSED` and
     `payload["state"] == "BREACHED"` and needs nothing else. WS1 adds no such rule: incidents are
     still opened only by deterministic `DetectionRule`s.
   * Catalog refresh builds a complete new snapshot first, refuses an empty estate, diffs it
     (added / removed / changed ids per kind, including dependencies), then rebinds the live
     catalog's dictionaries and the graph's indexes with no `await` in between. Rebinding, not
     mutating, means a collector iterating `catalog.items` keeps a consistent snapshot. Open
     incidents of an item that was removed get a timeline note, and automatic investigation skips
     them (a human decides what to do with them) instead of failing repeatedly.
5. **Investigations are background jobs** (`orqentis.runtime.investigations`). `POST
   /incidents/{id}/investigate` validates the request, returns `202 Accepted` with a job
   (`Location: /investigations/{job_id}`), and `GET /investigations/{job_id}` or `GET
   /incidents/{id}/investigation` report `QUEUED | RUNNING | SUCCEEDED | FAILED`, timestamps and a
   result summary. They are **idempotent per incident**: while a job is queued or running the same
   job is returned. The scheduler's auto-investigation submits through the same runner. A semaphore
   bounds concurrency; **the timeout starts when a job takes its slot, not when it is queued**, so a
   storm of incidents cannot make queued jobs expire unrun. A job that fails, times out or is
   cancelled puts its incident back to `DETECTED`. Auto-investigation gives up after N failed
   attempts per incident; cancellation by a shutdown is not an attempt. It also resets incidents
   orphaned in `INVESTIGATING` by a crashed process. Jobs live in memory (last 500 retained).
   `InvestigationService` is unchanged, and automation acts as `system:orqentis`; it can request
   approvals but cannot approve or execute.
6. **Incremental collection with watermarks per scope.** The `EventStore` port gains
   `get_watermarks(collector)` / `advance_watermarks(collector, {scope: ts})` (a small table keyed
   `(collector, scope)` in `LocalEventStore`; never moves backwards unless `force`; storage-agnostic
   so PostgreSQL can implement it). Collectors that observe many independent things (pipelines,
   semantic models, capacities) are *scoped*: each scope (item id) has its own watermark and window
   `[watermark - overlap, now]`, never further back than the maximum lookback. **A scope with no
   watermark is read from the maximum lookback**, so a pipeline that appears through a catalog refresh
   is not blind for the time before the collector's overall progress, even across a restart. A scope
   that fails keeps its watermark and does not hold back the others. A watermark advances only after
   its events were appended, and is persisted even if the run is cut short. A watermark that is
   *ahead* of the clock (the clock was set back) is not trusted: the maximum lookback is re-read once,
   the condition is logged, and the watermark is corrected. The overlap is safe because appends are
   de-duplicated by `dedupe_key`.
7. **Durable detection.** Storing events is not detecting them, and a run can be cut short between the
   two (a deploy, a lost lease, the 30-minute job timeout, a later collector failing). The event store
   therefore numbers every inserted event (`read_after(position)`) and keeps a named **cursor** per
   consumer (`get_cursor` / `advance_cursor`). `DetectionRunner` reads the events after its cursor, runs
   the detection engine, and moves the cursor only after the engine returned. `CollectionService` runs it
   after the ingestor *and* when the ingestor is interrupted or raises; the SLA sweep uses the same
   runner. Re-reading is harmless because the engine de-duplicates by item and correlation, and (for
   candidates without a correlation id) by the triggering event.
   * **The port contract is stronger than "a monotonic number".** A consumer advances its cursor to the
     highest sequence it was given and never looks back, so `read_after` may only return a **gap-free,
     committed prefix**: no event may be returned while a lower-numbered one can still become visible.
     SQLite satisfies this (one connection and a lock serialise writes, so `rowid` order is commit
     order; never `VACUUM` that table into a different order). **A PostgreSQL `bigserial` alone does
     not**: values are assigned at insert time, not commit time, so concurrent transactions (one process
     with a connection pool is enough) can commit out of order and a reader that has advanced past the
     higher value skips the lower one forever. WS6 must serialise appends (for example
     `pg_advisory_xact_lock` around the insert) or read only below the oldest in-flight transaction
     (`pg_snapshot_xmin(pg_current_snapshot())` against the transaction id stored with each row).
   * The activity failures of a run are stored *before* the run's own failure event. When an incident is
     opened (or matched), the engine therefore looks up the stored activity failures of that run and
     attaches them, so the symptoms never depend on where a batch or an interruption falls; failures that
     arrive later are attached as they come.
8. **No early stop when paging job instances.** The public documentation of *List Item Job
   Instances* (learn.microsoft.com, checked 2026-10-20) documents only a `continuationToken`,
   states that most items keep about the 100 most recent completed runs plus every active one, and
   says nothing about ordering. We therefore filter by watermark *after* reading and cap the pages
   (`ORQENTIS_FABRIC_JOB_INSTANCES_MAX_PAGES`). An early stop would also be unsound even with a
   documented start-time ordering: a long-running job that started before the watermark can finish
   after it and would sit on a page that is never read. A sound early stop needs both a documented
   order and a bound on run duration.
9. **A global Fabric call budget** (`orqentis.fabric.rest.budget`): a semaphore and a token bucket,
   applied inside `FabricHttp` to every Fabric and Power BI request. A `429`, or any retryable
   response carrying `Retry-After`, pauses *all* callers; retry backoff sleeps never hold a slot.
10. **Readiness is separate from liveness, and a failing build never crashes the process.** The
    lifespan starts a `Runtime` that builds the container in the background and returns after at most
    `ORQENTIS_STARTUP_GRACE_SECONDS`, so the port opens even if reading the estate takes minutes.
    `GET /health` is dependency-free liveness; `GET /health/ready` is `503` until the container is
    built and the first catalog load finished. *Any* exception from the build is caught, logged and
    retried with exponential backoff; what a failed attempt opened is closed. The probe is
    unauthenticated, so its `503` body carries only a fixed reason code (`starting` or `build_failed`)
    and the exception **type**; messages (which can hold paths) stay in the redacted logs. Other
    endpoints answer `503 NOT_READY` (with `Retry-After`) meanwhile. Bicep: startup and liveness
    probes on `/health`, readiness on `/health/ready`. Settings that fail validation (a missing
    `ORQENTIS_TENANT_ID` in rest mode, say) still stop the process at start: that is deploy-time
    misconfiguration, caught by the first probe failure, not a transient fault to retry.
11. **Retry of an interrupted investigation.** `EvidenceRecorder` skips evidence ids whose raw
    artefact already exists in the store. Raw artefacts are immutable, but an aborted attempt loses
    its in-memory evidence while keeping its files, so without this every retry (manual or
    automatic) collided with `EvidenceExistsError` on `EV001`.
12. **Deploys and two writers.** Container Apps in single-revision mode runs the old and the new
    revision side by side while an update rolls out, on the same Azure Files volume, each with its own
    in-process lease. The SQLite stores and the hash-chained audit file are single-writer, so this is a
    risk that only the PostgreSQL system of record (WS6: database lease, database-serialised audit
    chain) removes. Until then WS1 reduces it and says so plainly:
    * `ORQENTIS_SCHEDULER_STARTUP_DELAY_SECONDS` (default 120 outside `local`, 0 in `local`) makes a new
      process wait before it takes the lease or runs a job, so the revision being replaced drains first;
    * shutdown stops the scheduler, cancels its jobs and releases the lease before the app exits;
    * the local audit log re-reads the chain head when the file grew since it last wrote, so appends from
      two processes chain instead of forking (best effort: SMB has no byte-range locks, and two appends in
      the same instant can still collide). A file that gets *smaller* is never explained by another
      writer: the log refuses every further append (`AuditIntegrityError`) and `verify` reports it as
      truncated, instead of absorbing the loss and re-chaining over the gap;
    * **until WS6, deploy with the scheduler off (`schedulerEnabled = false`) or accept this risk.**
      Nothing is deployed before WS6.

## Alternatives considered

* **An external scheduler** (Container Apps Jobs, Azure Functions timers, Logic Apps) calling the
  API: more moving parts and credentials to run work that must share the process's catalog, watermark
  and lease state; the API would need to be exposed to it. Rejected for now.
* **A job framework** (APScheduler, Celery, Dramatiq): a broker or its own persistence, and a wider
  dependency surface, for four jobs and an in-memory job table. Rejected: the scheduler is
  a few hundred lines and the lease/job store ports are what matter for WS6.
* **A database lease and durable jobs now:** WS6 owns the database. The port is the hand-off.
* **Detection from the events a run just appended (the first version):** loses events whenever a run is
  interrupted, because stored events are de-duplicated as "not new" afterwards. Replaced by the cursor.
* **One watermark per collector (the first version):** a scope added later is blind before the
  collector's progress. Replaced by per-scope watermarks.
* **Keeping the container build in the lifespan with a longer startup probe:** it still crash-loops
  on a persistent build error and holds the port closed. Rejected.

## Consequences

* The app operates without being called; locally the scheduler is off unless enabled.
* `POST /incidents/{id}/investigate` now returns `202` and a job instead of the result (clients
  poll). `make demo` and the CLI call the service directly and are unchanged.
* One replica only: the lease and the job runner are in-process, and `maxReplicas` stays 1. WS6
  replaces the lease and persists jobs (and must stop treating "`INVESTIGATING` and not locally
  active" as orphaned without checking another replica's liveness).
* Investigation jobs do not survive a restart; their incidents are recovered after
  `investigation timeout + 5 min` and re-investigated.
* Detection processes every stored event once. On the first run after an upgrade the cursor starts at 0,
  so the existing events are read again; only those inside the detection horizon can open incidents and
  existing incidents are found by correlation.
* The event store's sequence relies on `rowid` in SQLite. The PostgreSQL adapter must honour the
  gap-free-committed-prefix contract of `EventStore.read_after` (§7); a bare `bigserial` is not enough.
* **Open questions for the orchestrator:** (1) verify live whether *List Item Job Instances* is
  newest-first and whether the runs' retention matches the documentation, then decide whether a
  duration-bounded early stop is worth adding; (2) the real Fabric/Power BI throttling quotas per
  identity, to set the budget defaults (4 concurrent, 5 requests/s, burst 10 are conservative guesses);
  (3) `SLA_ASSESSED` is only as fresh as the collectors: the SLA breach rule (WS7) should require a
  recent collector watermark before declaring a breach.
