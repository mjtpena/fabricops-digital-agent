# ADR-0013: In-process scheduler behind a leader lease, asynchronous investigation jobs and incremental collection

* Status: Accepted (production programme, WS1)
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
   monotonic **fencing token**). Only the holder runs jobs; the scheduler renews at half the TTL and
   cancels running jobs when it loses the lease or cannot reach the lease backend. We ship
   `InProcessLeaderLease` now (correct for one process) and a database-backed implementation arrives
   with PostgreSQL (WS6) without touching the scheduler. `renew` fails only if the lease was
   released or taken over, not merely because it expired, so a slow leader nobody replaced keeps its
   tenure.
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
     mutating, means a collector iterating `catalog.items` keeps a consistent snapshot.
5. **Investigations are background jobs** (`orqentis.runtime.investigations`). `POST
   /incidents/{id}/investigate` validates the request, returns `202 Accepted` with a job
   (`Location: /investigations/{job_id}`), and `GET /investigations/{job_id}` or `GET
   /incidents/{id}/investigation` report `QUEUED | RUNNING | SUCCEEDED | FAILED`, timestamps and a
   result summary. They are **idempotent per incident**: while a job is queued or running the same
   job is returned. The scheduler's auto-investigation submits through the same runner. A semaphore
   bounds concurrency, each job has a timeout, and a job that fails, times out or is cancelled puts
   its incident back to `DETECTED`. Auto-investigation gives up after N failed attempts per incident
   and resets incidents orphaned in `INVESTIGATING` by a crashed process. Jobs live in memory (last
   500 retained). `InvestigationService` is unchanged, and automation acts as `system:orqentis`; it
   can request approvals but cannot approve or execute.
6. **Incremental collection.** The `EventStore` port gains `get_watermark(collector)` and
   `advance_watermark(collector, ts)` (a small table in `LocalEventStore`; never moves backwards;
   storage-agnostic so PostgreSQL can implement it). A collector reads `[watermark - overlap, now]`,
   never further back than the configured maximum lookback, and its watermark advances only after its
   events were appended, so a failed collector retries the same window. The overlap is safe because
   appends are de-duplicated by `dedupe_key`.
7. **No early stop when paging job instances.** The public documentation of *List Item Job
   Instances* (learn.microsoft.com, checked 2026-10-20) documents only a `continuationToken`,
   states that most items keep about the 100 most recent completed runs plus every active one, and
   says nothing about ordering. We therefore filter by watermark *after* reading and cap the pages
   (`ORQENTIS_FABRIC_JOB_INSTANCES_MAX_PAGES`). An early stop would also be unsound even with a
   documented start-time ordering: a long-running job that started before the watermark can finish
   after it and would sit on a page that is never read. A sound early stop needs both a documented
   order and a bound on run duration.
8. **A global Fabric call budget** (`orqentis.fabric.rest.budget`): a semaphore and a token bucket,
   applied inside `FabricHttp` to every Fabric and Power BI request. A `429`, or any retryable
   response carrying `Retry-After`, pauses *all* callers; retry backoff sleeps never hold a slot.
9. **Readiness is separate from liveness, and a failing build never crashes the process.** The
   lifespan starts a `Runtime` that builds the container in the background and returns after at most
   `ORQENTIS_STARTUP_GRACE_SECONDS`, so the port opens even if reading the estate takes minutes.
   `GET /health` is dependency-free liveness; `GET /health/ready` is `503` with a redacted one-line
   reason until the container is built and the first catalog load finished. *Any* exception from the
   build is caught, logged and retried with exponential backoff; what a failed attempt opened is
   closed. Other endpoints answer `503 NOT_READY` (with `Retry-After`) meanwhile. Bicep: startup and
   liveness probes on `/health`, readiness on `/health/ready`.
10. **Retry of an interrupted investigation.** `EvidenceRecorder` skips evidence ids whose raw
    artefact already exists in the store. Raw artefacts are immutable, but an aborted attempt loses
    its in-memory evidence while keeping its files, so without this every retry (manual or
    automatic) collided with `EvidenceExistsError` on `EV001`.

## Alternatives considered

* **An external scheduler** (Container Apps Jobs, Azure Functions timers, Logic Apps) calling the
  API: more moving parts and credentials to run work that must share the process's catalog, watermark
  and lease state; the API would need to be exposed to it. Rejected for now.
* **A job framework** (APScheduler, Celery, Dramatiq): a broker or its own persistence, and a wider
  dependency surface, for four jobs and an in-memory job table. Rejected: the scheduler is
  ~300 lines and the lease/job store ports are what matter for WS6.
* **A database lease and durable jobs now:** WS6 owns the database. The port is the hand-off.
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
* **Open questions for the orchestrator:** (1) verify live whether *List Item Job Instances* is
  newest-first and whether the runs' retention matches the documentation, then decide whether a
  duration-bounded early stop is worth adding; (2) the real Fabric/Power BI throttling quotas per
  identity, to set the budget defaults (4 concurrent, 5 requests/s, burst 10 are conservative guesses);
  (3) `SLA_ASSESSED` is only as fresh as the collectors: the SLA breach rule (WS7) should require a
  recent collector watermark before declaring a breach.
