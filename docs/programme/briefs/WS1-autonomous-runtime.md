You are implementing **WS1: Autonomous runtime** in `mjtpena/fabricops-digital-agent`.
Base: `prod/ws0-ci-fix` (see *Branch, base and ADR numbers* below). Branch: `prod/ws1-autonomous-runtime`. Open one PR to `main` when done; never push to `main`.

## Context

FabricOps today does nothing on its own. Collection runs only via `POST /telemetry/collect`
(`src/orqentis/api/routers/system.py`). Investigation runs only via
`POST /incidents/{id}/investigate` (`src/orqentis/api/routers/incidents.py`), synchronously inside
the HTTP request, so a slow LLM investigation can exceed the Container Apps ingress timeout (~240 s).
Only mock mode collects at startup (`src/orqentis/api/app.py`, lifespan). The estate catalog is
loaded once at startup (`bootstrap.py`, `EstateCatalog.load`), so new workspaces and items stay
invisible until a restart. `TelemetryIngestor` uses a fixed 7-day lookback
(`src/orqentis/telemetry/collectors.py`), and `RestFabricClient.list_job_instances`
(`src/orqentis/fabric/rest/client.py`) paginates the full job history of every pipeline on every
run. There is no global concurrency or rate budget for Fabric calls. Startup enumerates the whole
estate before the app is ready, and the Bicep startup probe budget is ~155 s.

Later workstreams build on this: WS6 adds a database-backed `LeaderLease` and makes the app run as
2+ replicas, WS7 adds collectors and detection rules, WS8 adds notifications, WS9 adds metrics.

## Goal

The app operates autonomously and safely on a schedule: it collects and detects every N minutes,
auto-investigates new incidents as background jobs, sweeps SLAs and refreshes the estate catalog,
without ever running the same job twice concurrently, without blocking HTTP requests, and with
readiness separated from liveness so that startup never fails probes.

## In scope

1. **Scheduler port and in-process implementation.** A small scheduler started and stopped in the
   FastAPI lifespan, behind a pluggable `LeaderLease` port (`acquire(name, ttl) / renew / release`,
   holder id, fencing token or expiry). Ship an in-process implementation now; WS6 will add a
   database-backed one. Only the lease holder runs scheduled jobs. Jobs never overlap with
   themselves. A job failure is logged, audited where appropriate, and does not kill the scheduler.
   Use the existing `Clock` abstraction so that tests can drive time with a fake clock.
2. **Configurable jobs** (each can be enabled or disabled, with an interval):
   * collect + detect (default every 5 min);
   * auto-investigate new incidents (status `DETECTED`, not already being investigated);
   * SLA sweep: evaluate declared SLAs on a schedule (`incidents/sla.py`). In WS1 this only records
     SLA assessments as operational events. WS7 adds the detection rule for SLA breach, so design
     the event so a rule can consume it;
   * catalog refresh with diffing: reload workspaces and items, compute added, removed and changed,
     apply them to the live catalog and dependency graph, and log or audit the diff.
3. **Asynchronous investigations.** `POST /incidents/{id}/investigate` returns `202 Accepted` with a
   job id; `GET /investigations/{job_id}` (or `/incidents/{id}/investigation`) reports status
   (queued, running, succeeded, failed), timestamps and the result summary. Investigations are
   **idempotent per incident**: a second request while one is queued or running returns the
   existing job. The scheduler's auto-investigation uses the same job runner. Keep the
   investigation itself unchanged (`InvestigationService`). Update `make demo` / CLI only if they
   use the HTTP path (they currently call the service directly; keep that).
4. **Incremental collection.** Per-collector watermarks stored in the event store (add a small
   watermark table and methods to the `EventStore` port and `LocalEventStore`; keep the port
   storage-agnostic so WS6 can implement it in PostgreSQL). Replace the fixed 7-day lookback with
   "since watermark minus an overlap margin", with a configurable maximum initial lookback. Event
   deduplication (`dedupe_key`) stays, so overlap is safe.
5. **Stop paginating old job instances.** In `list_job_instances`, stop requesting further pages
   once every run on a page is older than the watermark, **only if** the Fabric "List Item Job
   Instances" API guarantees descending order by start time. Check the public documentation
   (learn.microsoft.com). If ordering is not documented, filter by watermark without an early stop,
   add a configurable page cap, and say so in the PR so the orchestrator verifies ordering live.
6. **Global Fabric call budget.** A shared concurrency limit (semaphore) and rate limit (token
   bucket) applied in the REST adapter's HTTP layer for all Fabric and Power BI calls, configurable,
   cooperating with the existing retry and `Retry-After` handling.
7. **Readiness separate from liveness.** `/health` stays as liveness (cheap, no dependencies).
   Add `/health/ready` that returns 503 until the container is built and the first catalog load
   finished, and 200 afterwards. If building the container fails (for example a missing or invalid
   contracts file), the process stays up, liveness stays 200, readiness returns 503 with a
   **redacted** reason, and the build is retried with backoff. No crash loop. Update the Bicep
   probes: liveness on `/health`, readiness on `/health/ready`, and a startup probe that no longer
   depends on estate enumeration.
8. **Settings and Bicep parameters** for all of the above with sensible defaults (scheduler on in
   deployed environments, off by default in `local` unless enabled; intervals; budgets; lookback;
   overlap). Keep `.env.example`, `settings.py`, `infra/bicep/main.bicep`, `infra/bicep/README.md`
   and `docs/` consistent.
9. **ADR** for the scheduler, lease and async-job design (`docs/adr/0013-...`, the number reserved for WS1).

## Out of scope

Database-backed lease and any new storage engine (WS6). New collectors, detection rules or
workflows (WS7). Notifications (WS8). Metrics and alerts (WS9). Auth changes (WS3). Environment
classification and contracts tooling (WS2). Changing `maxReplicas` (stays 1 until WS6).

## Coordination with parallel workstreams

* WS2 (portability) changes environment inference and contracts loading, and raises clear errors
  for missing or invalid contracts. Your readiness handling must treat **any** container-build
  exception as "not ready, retry"; do not depend on WS2's error types.
* WS3 (identity) adds middleware and changes `api/auth.py`. You own the lifespan and the health
  routes in `api/app.py`; keep your changes there contained.
* In `fabric/rest/client.py` change only the job-instance listing and the HTTP budget wiring. WS2
  changes environment resolution in the same file.

## Likely files

`src/orqentis/runtime/` (new: scheduler, lease port, in-process lease, job runner),
`src/orqentis/api/app.py`, `src/orqentis/api/routers/incidents.py`,
`src/orqentis/api/routers/system.py`, `src/orqentis/api/schemas.py`,
`src/orqentis/telemetry/collectors.py`, `src/orqentis/storage/event_store.py`,
`src/orqentis/fabric/rest/client.py`, `src/orqentis/fabric/rest/http.py` (or wherever the HTTP
helper lives), `src/orqentis/topology/catalog.py`, tests under `tests/unit` and
`tests/integration`. Hot files you may touch additively: `bootstrap.py`, `settings.py`,
`.env.example`, `infra/bicep/main.bicep`, `README.md`, `docs/architecture.md`.

## Acceptance criteria

1. Unit and integration tests driven by a fake clock prove that each job runs on its schedule,
   that only the lease holder runs jobs, that a job never runs twice concurrently (including when
   a run takes longer than its interval), and that a failing job does not stop the scheduler.
2. An integration test in mock mode shows an incident created by scheduled detection being
   investigated automatically, with no API call.
3. A test shows that a long investigation (a reasoner that blocks until released) never blocks an
   HTTP request: `POST /incidents/{id}/investigate` returns 202 immediately, other endpoints keep
   answering, and a duplicate request returns the same job.
4. Tests show that watermarks advance per collector, survive a restart (persisted), and bound
   the collection window; and that the early pagination stop (if implemented) stops at the right
   page using a mocked transport.
5. A test shows that the global budget caps concurrent Fabric calls at the configured value.
6. Tests show `/health` = 200 and `/health/ready` = 503 while the container build is failing or
   pending, and 200 after; and the build is retried.
7. Catalog refresh test: an item added to the mock estate between refreshes appears in the catalog
   and graph after the next refresh, and the diff is reported.
8. `make demo`, `make demo-all` still run offline; `make check`, `docker build`, `az bicep build`
   pass; `pytest -m security` passes.
