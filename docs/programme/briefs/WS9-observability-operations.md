You are implementing **WS9: Observability and operations** in `mjtpena/fabricops-digital-agent`.
Base: latest `origin/main`. Branch: `prod/ws9-observability-operations`. Open one PR to `main` when done; never push to `main`.

## Context

The app already emits structured JSON logs and OpenTelemetry spans (exported to Application Insights through the Azure
Monitor distro when `APPLICATIONINSIGHTS_CONNECTION_STRING` is set), and Container Apps sends console and system logs
to Log Analytics. Wave 1 added an autonomous scheduler with a leader lease (ADR-0013), collectors with watermarks and a
detection cursor, async investigation jobs, readiness separate from liveness, and audit-integrity refusal (the local
audit log refuses appends after it shrinks). WS6 (in parallel) moves storage to PostgreSQL and Blob and adds a restore
runbook. There are no metrics, alerts, workbook or operations guide.

## Goal

Operators know when FabricOps is down, unhealthy, blind or tampered with, through alerts that fire on real conditions,
and have a written guide for running it.

## In scope

1. **OpenTelemetry metrics** (instruments behind a small metrics module; no SDK imports outside adapters/bootstrap):
   collection duration and errors per collector, events ingested, incidents opened (by category, severity, environment),
   investigation duration and outcome, tool errors by tool, LLM tokens and estimated cost (when the agent reasoner is
   on), approvals pending (gauge), notifications sent/failed (if WS8 has merged; otherwise leave a hook), scheduler job
   runs/failures/overruns, lease holder changes, and a **heartbeat**: last successful collection time.
2. **Audit verification job**: a scheduled job that verifies the audit chain (and the latest immutable anchor once WS6
   provides it) and emits a metric plus a log event on failure.
3. **Alerts in Bicep** (scheduled query rules or metric alerts on Application Insights / Log Analytics) and an
   **action group** (email receivers and an optional Teams Workflows webhook, both parameters; no secrets in parameter
   files):
   * app down (no healthy replica / availability),
   * restart loop (container restarts over a threshold in a window, from `ContainerAppSystemLogs_CL` or platform metrics),
   * 5xx rate above a threshold,
   * collector errors,
   * **heartbeat**: no successful collection in 15 minutes,
   * audit verification failure.
   Thresholds and windows are parameters with documented defaults; every alert has a runbook link.
4. **Workbook** (Bicep) with the key charts: collections and errors, detection latency (failure time to incident
   opened), incidents by category, investigation duration, approvals pending, LLM tokens/cost, restarts, 5xx.
5. **`docs/operations.md`**: on-call guide; what each alert means and the first three things to check; restore (link
   WS6's runbook); key and secret rotation (Key Vault references, federated credentials, test SP secret); upgrade and
   rollback through CD (redeploy a previous digest); data retention (Log Analytics, App Insights, PostgreSQL PITR, Blob
   immutability); scaling and the leader lease; how to pause the scheduler safely. Plus a **cost guide**
   (`docs/cost.md`): every billable resource, its default SKU, list price source, and how to keep a sandbox cheap
   (pause the Fabric capacity, burstable PostgreSQL, Log Analytics daily cap).
6. Settings, `.env.example`, Bicep parameters, `infra/bicep/README.md` consistent; ADR for the metrics and alerting design.

## Out of scope

Deploying or firing alerts in Azure (the orchestrator does that after owner approval). Storage adapters (WS6),
notifier (WS8), new detection (WS7).

## Coordination

WS6 rewrites storage wiring in `bootstrap.py` and the storage parts of `infra/bicep/main.bicep`; keep your Bicep changes
in a new module (e.g. `infra/bicep/modules/monitoring.bicep`) referenced additively from `main.bicep`, and your
`bootstrap.py` changes additive. Merge `origin/main` before opening the PR.

## Acceptance criteria

1. Tests: each metric is emitted with the right attributes in the right situations (fake clock; in-memory metric reader);
   the heartbeat advances only on successful collection; the audit verification job reports a tampered chain.
2. `az bicep build` / `build-params` pass with the monitoring module; every alert has a severity, a window, a threshold
   parameter, the action group and a runbook link; `what-if`-ready (no hard-coded resource ids).
3. `docs/operations.md` and `docs/cost.md` exist and match the deployed resources.
4. `make check`, `make demo` (offline; metrics exporters off by default), `pytest -m security`, `docker build` pass.
5. Live (done by the orchestrator in `dev` after merge): each alert fires on a deliberate failure and reaches the
   action group.
