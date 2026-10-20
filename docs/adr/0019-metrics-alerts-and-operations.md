# ADR-0019: Metrics, alerts, a scheduled audit verification and the operations guide

* Status: Accepted (production programme, WS9)
* Date: 2026-10-20

## Context

FabricOps now runs on its own (ADR-0013): a scheduler collects, detects and investigates without being called. That
changes what an operator needs. Nobody is watching a request that fails; the failure that matters is *silence* (the
scheduler lost its lease, a credential expired and every collector fails, the process restarts in a loop) or *tampering*
(the audit chain no longer verifies). The application emitted structured logs and OpenTelemetry spans, but no metrics, no
alerts, no dashboard, no scheduled integrity check and no written operating procedure. `GET /audit/verify` existed, but
only a person calling it ever learned that the chain was broken.

The aim: operators know when FabricOps is down, unhealthy, blind or tampered with, through alerts that fire on real
conditions, and they have a guide for running it.

## Decision

1. **Metrics behind one class** (`orqentis.observability.metrics.Metrics`). It is the only code that names an OpenTelemetry
   instrument; components receive it by constructor argument (`metrics: Metrics = DISABLED`) and call typed helpers such as
   `collector_run(...)` or `incident_opened(...)`. It uses the OpenTelemetry *API* only, so it records into whatever meter
   provider is installed. Tests build `Metrics(provider.get_meter(...))` on a private provider with an in-memory reader and a
   fake clock. The full instrument list, units and attributes are in the module docstring and in `docs/operations.md`.
   * **Bounded attributes.** A value is an enum member, one of our own collector/job/tool names, or `unknown`. A tool name the
     model made up, an item name or an error message from Fabric never becomes an attribute: that keeps cardinality small and
     untrusted metadata (invariant 8) out of telemetry.
   * **Nothing is exported by default.** Without `APPLICATIONINSIGHTS_CONNECTION_STRING` and without
     `ORQENTIS_OTEL_CONSOLE_EXPORTER` there is no meter provider and every call is a no-op, so `make demo` and the tests export
     nothing. With the connection string the Azure Monitor distro exports traces *and* metrics (counters and histograms as
     per-interval deltas; verified in the exporter source); with the console flag `tracing.py` installs a console metric
     reader. No new setting was needed for this.
2. **The heartbeat is the last collection that did not fail outright.** `orqentis.collection.heartbeat` (a gauge: Unix time)
   and the counter `orqentis.collection.runs{outcome}` move only for a run that finished, ran detection, and in which at least
   one collector or scope succeeded (`success`, or `degraded` when some failed). A run in which every collector fails (an
   expired credential, a Fabric outage), one that raises, one that is cancelled, and one whose detection fails are `failed` or
   `cancelled` and do **not** advance it. The naive version (advance on "the run returned") would have kept the heartbeat green
   through exactly the failure it exists to catch, because the ingestor deliberately swallows per-collector Fabric errors.
   `degraded` still advances it on purpose: with Viewer-only access the semantic-model refresh collector fails every time
   (programme decision D9), and a permanently red heartbeat would hide the day it really stops. A collector that keeps failing
   is the *collector errors* alert's job.
3. **A scheduled audit verification job** (`audit-verify`, hourly by default, `runtime/audit_verification.py`) verifies the
   hash chain and, when the storage layer provides one, the latest immutable anchor (`AuditAnchorVerifier`; WS6 supplies the
   implementation and passes it in `bootstrap.py`). It emits `orqentis.audit.verifications{result=valid|invalid|error}`, and on
   failure one ERROR log line, `audit chain verification failed`, to the console log. A read that fails is retried twice before
   it counts as `error` (a reader meeting a half-written record must not page anyone); an invalid chain is never retried. It
   audits only *changes* of health, as the scheduler does, and tolerates a log that refuses appends.
4. **Alerts in Bicep, in a new module** (`infra/bicep/modules/monitoring.bicep`, referenced additively from `main.bicep`):
   an action group (email receivers, optional Teams Workflows webhook) and six alerts, each with a severity, a window, a
   threshold parameter, the action group and a runbook link, plus a workbook.

   | Alert | Kind | Signal | Sev |
   |---|---|---|---|
   | app down | metric alert | platform metric `Replicas` (maximum below 1 for the window) | 1 |
   | restart loop | log alert | `max(process starts, container terminations)` in the window | 1 |
   | 5xx rate | log alert | share of `AppRequests` answering 5xx, with a minimum request count | 2 |
   | collector errors | log alert | `collector.runs{outcome=error}` per collector | 2 |
   | collection heartbeat | log alert | no `collection.runs{outcome in (success, degraded)}` for the window | 1 |
   | audit verification failure | log alert | `audit.verifications{result!=valid}` or the console error line | 0 |

   * **Log alerts read the workspace** (one scope, `AppMetrics` + Container Apps tables), not the Application Insights
     component, because the restart and audit signals live in Container Apps tables. They are deployed with
     `skipQueryValidation`: the tables exist only after the first ingested row, and a deployment must not fail on a new
     environment.
   * **Console-log alerts trust fields, not phrases.** Some log lines carry text chosen by Fabric (a collector's error
     message) or by the model (the exception text of a failed reasoner), so a query that matched a phrase anywhere in a line
     would let a Fabric error that quotes `audit chain verification failed` raise the Sev 0 tampering alert, or one that quotes
     `orqentis process started` inflate the restart count. The queries parse `Log_s` as JSON and match the exact `logger` and
     `message` fields (`has` only narrows the rows first). That is sound because every log call in the application passes a
     constant message and puts variable text in extra fields (a test walks the source to keep it so), and because the JSON
     formatter escapes newlines, so one record is one line and a forged line cannot be smuggled inside another.
   * **Windows are what Azure accepts.** Azure takes only fixed durations for a log alert window (5, 10, 15, 30, 45 minutes, 1-6
     hours, 1 or 2 days) and refuses anything else, so the window parameters are `@allowed` lists, minutes are mapped to ISO
     durations through an explicit table (never formatted into `PT<n>M`), a window shorter than the evaluation frequency stops
     the deployment with a message that names it, and the audit window is derived from the verification interval (twice the
     interval, rounded up to an allowed window).
   * **The audit alert is sticky.** It has `autoMitigate: false` and re-notifies at most every six hours: after a restart the
     in-memory proof of a truncated log is gone and the next verification can look clean, so a person, not the clock, resolves
     a tampering alert.
   * **Silence is detected by absence.** The heartbeat query counts the successful-run rows and fires on `0`
     (`count()` over an empty set is `0`, a `sum()` would be null and never fire). It is switched off in the template while
     `schedulerEnabled` or `collectEnabled` is false, so a deliberately paused scheduler does not page anyone.
   * **Restart loop uses two independent signals.** The app logs `orqentis process started` once per process start (console
     logs reach the workspace through the platform, with no dependence on the metrics exporter, and a crash loop may die before
     a metric is exported). The platform's own termination events are the second signal. `RestartCount` was rejected: it is
     the cumulative restarts since the replica was created, so a metric alert on it cannot express "3 in 15 minutes" and
     stays red for the life of a replica that restarted three times last month.
   * **Receivers are never in the repository.** `alertEmailReceivers` and `alertTeamsWebhookUri` (`@secure()`) are
     deployment-time inputs: `env/*.bicepparam` read `ALERT_EMAIL_RECEIVERS` and `ALERT_TEAMS_WEBHOOK_URI` from the deployer's
     environment and default to empty. A test refuses a webhook URL or an address in any parameter file.
   * **Every alert links to a runbook** (`alertRunbookBaseUrl` + `#alert-...`, one section of `docs/operations.md` per alert;
     a test keeps the anchors and the headings in step) in the rule description and in the notification's custom properties.
   * **No hard-coded resource ids**: everything derives from the workspace and Container App ids that `main.bicep` passes, so
     `what-if` works in any subscription and cloud.
5. **Logs reach the workspace through the console, not twice.** `configure_logging` sets `propagate = False` on the
   `orqentis` logger, so the Azure Monitor log handler on the root logger does not receive application logs and
   `AppTraces` does not contain them. This is existing behaviour and deliberately kept: shipping the same lines both as
   console logs and as `AppTraces` would double the ingestion bill. Log-based signals therefore read
   `ContainerAppConsoleLogs_CL`; metric-based ones read `AppMetrics`.
6. **`docs/operations.md` and `docs/cost.md`.** The operations guide covers on-call, what each alert means and the first three
   things to check, restore, key and secret rotation, upgrade and rollback by digest, retention, scaling and the lease, and
   pausing the scheduler. The cost guide lists every billable resource, its default SKU, the list-price source and the levers
   that keep a sandbox cheap.
7. **Notifications are counted when they end.** WS8's dispatcher (ADR-0018) calls `Metrics.notification(channel, outcome)` once per
   message: `sent`, `failed` (refused for good, or the attempts ran out), `dropped` (the bounded outbox was full) or `abandoned`
   (the process stopped first), with the adapter's name as the channel. Retries are attempts and are not counted. The call comes
   before the audit write, so a failing audit trail cannot hide a failed delivery. There is no alert on it yet: a webhook that
   stays down shows in the workbook and in the audit trail, and an alert on `failed` is a follow-up once the owner has seen the
   real rate.
8. **Estimated model cost** needs prices, which differ per model, region and agreement and go stale, so they are settings
   (`ORQENTIS_LLM_INPUT_COST_PER_MILLION_TOKENS`, `..._OUTPUT_...`) and default to 0 (not configured): tokens are always
   counted, `orqentis.llm.cost` is emitted only when a price is set.

## Alternatives considered

* **Metric alerts on custom metrics** (the distro can send OpenTelemetry metrics to the Azure Monitor metrics store, where a
  metric alert evaluates every minute for less money). Rejected for now: it needs an opt-in namespace, restricts dimensions,
  and the same instruments would then live in two places. The log alerts evaluate every 5 minutes, which fits a collection
  that runs every 5 minutes.
* **A metric alert on `RestartCount`**, see above. **An availability (web) test** for app down: it only works for a publicly
  reachable endpoint (`ingressMode` `internal` and `vnet` are not), and is a follow-up for deployments that expose one.
* **Dynamic thresholds**: opaque for rare events such as an integrity failure. Static, parameterised thresholds are explicit.
* **A Prometheus endpoint / OTLP collector sidecar**: more moving parts than a single-replica app needs.
* **Alerting from the workbook or from Application Insights smart detection**: not deterministic enough to promise "fires on a
  deliberate failure".

## Consequences

* The KQL cannot be executed offline. The module is validated by `az bicep build`, structural tests (every rule has a
  severity, window, threshold parameter, the action group and a runbook link whose anchor exists) and tests that check the
  metric names and attribute keys the queries read against what the code emits. Whether each alert fires and reaches the
  action group is the live acceptance test in `dev` (`docs/operations.md`, *Test-firing the alerts*).
* The alerts cost money: five log alert rules at a 5-minute evaluation frequency are about USD 7.50 a month at list price
  (USD 2.50 at 15 minutes), more than the programme's earlier USD 1-3 estimate for one deliberate sandbox (`docs/cost.md`).
* Blind spots, stated: **app down** sees a replica that does not exist, not one that runs without being ready (that appears as
  a restart loop, a missing heartbeat or 5xx), and if the `Replicas` metric reports no data for an app without an active
  revision it cannot fire at all, in which case the heartbeat (15 minutes) is the backstop; the 5xx alert counts what the *app* answered, so 503s produced by the ingress
  while no replica is ready are covered by app down and the heartbeat; the heartbeat depends on the metrics pipeline, so a
  broken exporter looks like a dead scheduler (the first check says so).
* With the PostgreSQL storage (WS6) the app runs two or more replicas: only the lease holder emits the heartbeat and
  `scheduler.leader` is 1 on exactly one replica. Alerts sum over replicas, so they keep working. The audit verification job
  then reads the database-serialised chain and should pass the immutable anchor to the task.
* `CollectionService` and the ingestor gained optional `metrics`/`clock` arguments, `IngestionResult` gained `units_ok` and
  `units_failed`. Existing callers are unchanged.
* The CD pipeline does not yet supply `ALERT_EMAIL_RECEIVERS` / `ALERT_TEAMS_WEBHOOK_URI`. A deployment without them leaves the
  action group without receivers, and removes receivers added by hand, because the template owns the action group. Wiring them
  from GitHub *environment secrets* is a decision for the owner (invariant 9 forbids credentials in CI variables; a
  webhook URL carries its signature); the alternative that keeps CI free of them is a Key Vault `getSecret` reference in the
  parameter file once WS6 provides the vault.
* **Open questions for the orchestrator:** (1) the exact `Reason`/`Log` text of Container Apps platform termination events is
  not documented; the restart-loop alert also counts the application's own start line, which does not depend on it, but check
  the platform leg with `ContainerAppSystemLogs_CL | summarize count() by Reason_s` during the dev test fire. (2) Whether the
  `Replicas` metric reports `0` (rather than nothing) for a deactivated app; if it reports nothing, add an availability test.
