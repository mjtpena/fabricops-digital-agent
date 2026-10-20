# Operations guide

How to run a deployed Orqentis FabricOps: what to watch, what each alert means and what to do first, how to restore,
rotate, upgrade, roll back and pause it. It is written for the on-call engineer and the platform owner of an
environment (`dev`, `test` or `prod`), and it describes what the repository deploys today. Things that arrive with the
PostgreSQL storage (WS6) are marked **(after WS6)**. The design of the metrics and alerts is
[ADR-0019](adr/0019-metrics-alerts-and-operations.md); what everything costs is [`cost.md`](cost.md); how a change
reaches an environment is [`delivery.md`](delivery.md); the Bicep parameters are in
[`../infra/bicep/README.md`](../infra/bicep/README.md).

Commands below use `RG` for the environment's resource group (for example `rg-fabricops-dev`) and `PREFIX` for the
template's `prefix` (for example `fabricops-dev`), so the Container App is `ca-$PREFIX`, the workspace `log-$PREFIX`
and the action group `ag-$PREFIX`.

## 1. What FabricOps does on its own

One Container App runs one process. The scheduler inside it, gated by a leader lease
([ADR-0013](adr/0013-scheduler-lease-and-async-investigation-jobs.md)), runs five jobs:

| Job | Default interval | What it does | If it stops |
|---|---|---|---|
| `collect-detect` | 5 min | Reads Fabric (pipeline runs, semantic-model refreshes, capacity), stores events, runs the detection rules | FabricOps is blind: no new incidents (the heartbeat alert) |
| `auto-investigate` | 1 min | Investigates `DETECTED` incidents as background jobs | Incidents stay `DETECTED` until someone investigates them through the API |
| `sla-sweep` | 15 min | Records SLA state changes as events | SLA breaches are not recorded |
| `catalog-refresh` | 60 min | Reloads workspaces, items and dependencies | New items are not monitored until the next refresh or restart |
| `audit-verify` | 60 min | Verifies the audit hash chain | Tampering would go unnoticed until someone calls `GET /audit/verify` |

Incidents are opened only by deterministic detection rules. The model never monitors; it explains what the rules found.
Nothing is changed in Fabric without a human approval (read `security.md`).

**Healthy looks like:** `GET /health` answers `200`; `GET /health/ready` answers `200` with `"scheduler": "leader"` (the
replica that runs the jobs) or `"standby"`; the workbook shows a collection run every 5 minutes with outcome `success`;
the pending-approvals line moves when people decide.

`/health` and `/health/ready` need no token when `anonymousHealthEndpoints` is true (the default). `503` from
`/health/ready` is the container still being built (`"reason": "starting"`) or its build failing and being retried with
backoff (`"build_failed"`, with the exception *type*; the message is in the console log).

## 2. Where to look

| You want | Look here |
|---|---|
| The overview | Workbook **Orqentis FabricOps (`PREFIX`)**: Azure Monitor > Workbooks > the resource group |
| What the alerts are doing | Azure Monitor > Alerts, or `az monitor scheduled-query list -g $RG -o table` |
| Application metrics | Log Analytics workspace `log-$PREFIX`, table `AppMetrics` (name = instrument, attributes in `Properties`) |
| Requests and exceptions | `AppRequests`, `AppExceptions`, `AppDependencies` (the Application Insights tables of the same workspace) |
| What the app printed | `ContainerAppConsoleLogs_CL` (`Log_s` holds one JSON log line); `az containerapp logs show -g $RG -n ca-$PREFIX --tail 100` |
| What the platform did | `ContainerAppSystemLogs_CL`: revision provisioning, probes, volume mounts, terminations |
| Which revision runs which image | `az containerapp revision list -g $RG -n ca-$PREFIX -o table` |

Run a query from the shell (`$WORKSPACE_ID` is the workspace's *customer id*, `az monitor log-analytics workspace show
-g $RG -n log-$PREFIX --query customerId -o tsv`):

```bash
az monitor log-analytics query --workspace "$WORKSPACE_ID" --analytics-query "
  AppMetrics | where TimeGenerated > ago(1h) | where Name == 'orqentis.collection.runs'
  | summarize runs = sum(Sum) by outcome = tostring(Properties.outcome)" -o table
```

Logs reach the workspace through the container's console output only. Application logs are deliberately *not* also sent
to `AppTraces` (that would double the ingestion cost); spans and metrics go through the OpenTelemetry exporter.

## 3. The metrics

Recorded through OpenTelemetry (`src/orqentis/observability/metrics.py`), exported to Application Insights when
`APPLICATIONINSIGHTS_CONNECTION_STRING` is set (the template always sets it) and to the console with
`ORQENTIS_OTEL_CONSOLE_EXPORTER=true`. Nothing is exported otherwise. Counters and histograms arrive as per-minute
deltas (`OTEL_METRIC_EXPORT_INTERVAL`, default 60000 ms), so `sum(Sum)` over a window is the number of events in it.
Attribute values are bounded: enum members and our own collector, job and tool names, never Fabric names or error text.

| Metric | Kind | Attributes | Meaning |
|---|---|---|---|
| `orqentis.collector.duration` (s) | histogram | `collector` | Time one collector took in one run |
| `orqentis.collector.runs` | counter | `collector`, `outcome` = `ok`/`error` | `error` when the collector, or any of its scopes, raised a Fabric error |
| `orqentis.collector.errors` | counter | `collector` | Failed collector runs or failed scopes (one per failing pipeline or model) |
| `orqentis.events.ingested` | counter | `collector` | Events that were new to the event store |
| `orqentis.collection.runs` | counter | `outcome` = `success`/`degraded`/`failed`/`cancelled` | One per collect + detect run (below) |
| `orqentis.collection.heartbeat` (s) | gauge | none | Unix time of the last successful collection |
| `orqentis.incidents.opened` | counter | `category`, `severity`, `environment` | Incidents opened by a detection rule |
| `orqentis.incidents.detection_latency` (s) | histogram | same | Failure event to incident opened |
| `orqentis.investigation.runs` | counter | `outcome` = `succeeded`/`failed`/`timeout`/`cancelled`, `trigger` = `API`/`SCHEDULER` | Investigation jobs |
| `orqentis.investigation.duration` (s) | histogram | same | Run time of an investigation job |
| `orqentis.tool.calls` | counter | `tool` (a registered tool or `unknown`), `status` | Every tool invocation |
| `orqentis.tool.errors` | counter | `tool` | Invocations with status `ERROR` (refusals are not errors) |
| `orqentis.llm.tokens` | counter | `model`, `direction` = `input`/`output` | Tokens used by the Agent Framework reasoner |
| `orqentis.llm.cost` (USD) | counter | `model` | Estimate from `ORQENTIS_LLM_*_COST_PER_MILLION_TOKENS`; absent when they are 0 |
| `orqentis.reasoner.fallbacks` | counter | `reasoner` | The LLM reasoner failed and the rule-based one answered |
| `orqentis.approvals.pending` | gauge | none | Approval requests waiting for a human (expired ones excluded) |
| `orqentis.scheduler.job.runs` | counter | `job`, `outcome` = `ok`/`failed`/`timeout`/`cancelled` | Scheduled job runs |
| `orqentis.scheduler.job.failures` | counter | `job`, `reason` = `failed`/`timeout` | Runs that failed or timed out |
| `orqentis.scheduler.job.overruns` | counter | `job` | Slots skipped because the previous run was still going |
| `orqentis.scheduler.job.duration` (s) | histogram | `job`, `outcome` | Job run time |
| `orqentis.scheduler.lease.changes` | counter | `event` = `acquired`/`lost` | This replica gained or lost the leader lease |
| `orqentis.scheduler.leader` | gauge | none | `1` while this replica holds the lease |
| `orqentis.notifications` | counter | `channel`, `outcome` | Hook for the notifier (WS8); nothing emits it yet |
| `orqentis.audit.verifications` | counter | `result` = `valid`/`invalid`/`error` | Scheduled audit verifications |

**What a collection outcome means.** `success`: every collector and scope worked. `degraded`: some failed, at least one
worked (FabricOps sees part of the estate); the heartbeat still advances, and the *collector errors* alert is what
tells you. `failed`: everything that was tried failed, or the run raised (an expired credential or a Fabric outage looks
like this); the heartbeat does **not** advance. `cancelled`: a deploy, a lost lease or the job timeout cut the run short.

## 4. Alert receivers

The action group `ag-$PREFIX` notifies the people in two **deployment-time** inputs. They are never stored in the
repository or in a parameter file:

| Input | Where it is read | Form |
|---|---|---|
| `ALERT_EMAIL_RECEIVERS` | environment of whoever runs the deployment, read by `infra/bicep/env/*.bicepparam` | comma separated addresses |
| `ALERT_TEAMS_WEBHOOK_URI` | same | a Microsoft Teams **Workflows** webhook URL ("When a Teams webhook request is received") |

```bash
export ALERT_EMAIL_RECEIVERS="oncall@example.com,platform@example.com"   # your team's addresses
export ALERT_TEAMS_WEBHOOK_URI=...                                         # from your secret store, never typed into a file
az deployment group create -g "$RG" -p infra/bicep/env/dev.bicepparam      # with the CD variables of delivery.md exported
```

* The webhook URL carries its own signature, so treat it as a secret: keep it in the deploying person's secret store,
  rotate it by recreating the Workflow and redeploying, and do not put it in a repository, a parameter file or a CI
  variable. The action group stores it in clear and shows it to anyone who can read the action group, so keep Reader on
  the resource group to the operators.
* The action group sends the common alert schema. A Teams Workflow shows a chat message from it only if its flow reads
  `data.essentials.alertRule`, `data.essentials.severity` and the `runbook` custom property and posts them; build the
  flow once and test it with **Test action group** in the portal.
* **The CD pipeline does not set these variables yet.** The template owns the action group, so a CD run of `main.bicep` with
  both empty leaves it without receivers and removes any added by hand. Until the pipeline supplies them (a decision for the
  owner: the webhook is a secret, and invariant 9 keeps credentials out of CI variables), set them in the shell that runs
  deployments, or accept that CD runs reset the receivers.
* Check what is configured: `az monitor action-group show -g $RG -n ag-$PREFIX --query "{email:emailReceivers[].emailAddress, webhook:webhookReceivers[].name}"`;
  the deployment output `alertEmailReceiverCount` says how many email receivers the last deployment set (0 means alerts
  notify nobody by email).

## 5. The alerts

Six rules, all Sev 0-2, all in the resource group, all using the action group. Log alerts are evaluated every
`alertEvaluationFrequencyMinutes` (5) over a window; they are stateful (`autoMitigate`): a rule opens one alert and resolves it
when the condition clears, so you are not re-notified at every evaluation. Every rule's description and notification carries a link to its
section below. **Triage order when several fire:** audit verification, then app down, then restart loop, then heartbeat,
then collector errors, then 5xx: the earlier ones usually explain the later ones.

| Alert (`$PREFIX-...`) | Sev | Fires when (defaults) | Parameters |
|---|---|---|---|
| `audit-verification-failure` | 0 | a scheduled audit verification failed or could not read the log | `alertAuditFailureWindowMinutes` 65 |
| `app-down` | 1 | no running replica for 5 min | `alertAppDownWindowMinutes` 5 |
| `restart-loop` | 1 | 3 process starts or container terminations in 15 min | `alertRestartWindowMinutes` 15, `alertRestartThreshold` 3 |
| `collection-heartbeat` | 1 | no successful collection for 15 min | `alertHeartbeatMinutes` 15 |
| `collector-errors` | 2 | one collector had 2 failing runs in 15 min (one alert per collector) | `alertCollectorErrorWindowMinutes` 15, `alertCollectorErrorRuns` 2 |
| `5xx-rate` | 2 | 5 % or more of at least 20 requests in 10 min answered 5xx | `alertHttp5xxWindowMinutes` 10, `alertHttp5xxRatePercent` 5, `alertHttp5xxMinRequests` 20 |

Change a threshold or window by editing the parameter file (or passing `-p alertRestartThreshold=5`) and redeploying; the
deployed rule is replaced in place.

### Alert: app down

**Meaning.** The platform metric `Replicas` of the Container App stayed below 1 for the whole window: no replica is
running, so the API and the scheduler are both off. Sev 1.

**First three things to check.**

1. Is it deliberate? `az containerapp show -g $RG -n ca-$PREFIX --query "properties.{state:runningStatus,latest:latestRevisionName,ready:latestReadyRevisionName}" -o json`
   and `az containerapp revision list -g $RG -n ca-$PREFIX --query "[].{name:name,active:properties.active,health:properties.healthState,replicas:properties.replicas,created:properties.createdTime}" -o table`.
   A revision someone deactivated, or a deployment still provisioning, explains it.
2. Why did the revision not come up? `ContainerAppSystemLogs_CL | where ContainerAppName_s == 'ca-$PREFIX' | where TimeGenerated > ago(1h) | project TimeGenerated, RevisionName_s, Log_s | order by TimeGenerated desc`
   shows `Error provisioning revision ... ErrorCode: ErrImagePull | Timeout | ContainerCrashing` and volume-mount errors.
   `ErrImagePull`: the reader identity lost `AcrPull` or the digest does not exist. `Error mounting volume`: the storage
   account key was rotated without a redeploy (section 9) or the account is unreachable.
3. Did a deployment cause it? Look at the last `cd.yml` run for the environment. If so, roll back to the previous digest
   (section 10), then investigate.

**Limits.** The alert does not see a replica that exists but is not ready or keeps crashing; that is the *restart loop*,
*heartbeat* and *5xx* alerts. If `Replicas` reports nothing (instead of `0`) for a deactivated app the alert cannot fire;
the dev test fire (section 7) checks that.

### Alert: restart loop

**Meaning.** The app process started, or its container was terminated, at least `alertRestartThreshold` (3) times in the
window (15 min). One deployment is one start, so three in a quarter of an hour is a crash loop, not a rollout. The count is
the larger of two independent signals: the application's own `orqentis process started` console line (it survives a
metrics outage and a crash before the first metric is exported) and the platform's termination events in the system log.
Sev 1.

**First three things to check.**

1. The last lines before each start. `az containerapp logs show -g $RG -n ca-$PREFIX --tail 200` (or
   `ContainerAppConsoleLogs_CL | where Log_s !has 'orqentis process started' | where TimeGenerated > ago(30m) | project TimeGenerated, Log_s | order by TimeGenerated desc`).
   A settings error (`ORQENTIS_...` validation, `dev_headers` outside `local`) or `Contracts file is invalid` names the exact
   problem and stops the process on purpose: that is deploy-time misconfiguration, fix the parameter or the file. A
   failed container *build* does **not** crash the process; it is retried in the background and shows as
   `/health/ready` `503`.
2. Platform kills. `ContainerAppSystemLogs_CL | where ContainerAppName_s == 'ca-$PREFIX' | where Log_s has_any ('terminated', 'OOM', 'Probe') | project TimeGenerated, RevisionName_s, Log_s`.
   `OOMKilled` means the container's memory is too small (`containerMemory`, 1 Gi by default, 0.5 Gi in `dev`): raise it with its
   matching `containerCpu` pair. Failing probes: the startup probe allows about 55 s for the port to open (5 s delay, then 10 tries 5 s apart); liveness
   restarts a process that does not answer `/health` three times in a row.
3. The last change. A restart loop that began at a deployment is rolled back by redeploying the previous digest (section 10).

### Alert: 5xx rate

**Meaning.** At least `alertHttp5xxRatePercent` (5 %) of the requests the app handled in the window (10 min) answered 5xx,
counting windows with at least `alertHttp5xxMinRequests` (20) requests only. Health probes are excluded. Note that
`503 NOT_READY` (the container is being built or its build is failing) is a 5xx. Sev 2.

**First three things to check.**

1. Readiness. `curl -s https://<fqdn>/health/ready`: `503` with `starting` or `build_failed` explains a burst of `NOT_READY`.
   A build that keeps failing (Fabric unreachable, a bad contracts file) is in the console log with its exception type.
2. Which endpoint and which status. `AppRequests | where TimeGenerated > ago(30m) | where Name !has '/health' | summarize n = sum(ItemCount), errors = sumif(ItemCount, toint(ResultCode) >= 500) by Name, ResultCode | order by errors desc`.
3. The exceptions behind it. `AppExceptions | where TimeGenerated > ago(30m) | summarize count() by ProblemId, OuterMessage | order by count_ desc` (messages are redacted before they are logged, but treat them as untrusted text).
   A burst after a deployment is rolled back (section 10).

### Alert: collector errors

**Meaning.** One collector (the dimension `collector`: `pipeline-runs`, `semantic-model-refreshes`, `capacity-metrics`,
...) had at least `alertCollectorErrorRuns` (2) failing runs in the window (15 min). A run fails when the collector, or any
pipeline or model it covers, raised a Fabric error. FabricOps is blind to what that collector observes, though it keeps
collecting the rest (the run is `degraded`, the heartbeat still advances). Sev 2.

**First three things to check.**

1. What the error is. `ContainerAppConsoleLogs_CL | where Log_s has 'collector failed' | where TimeGenerated > ago(30m) | project TimeGenerated, Log_s | take 20`:
   each line names the collector (or `collector:item`) and a redacted error with its HTTP status and Fabric error code.
2. Classify it. `401`/`403`: identity or permission (the reader identity lost a workspace role, a service principal secret
   expired, a Fabric tenant setting changed). **A permanent `403` from `semantic-model-refreshes` is known**: refresh history
   needs more than Viewer (programme decision D9, `infra/bicep/README.md` section 5); it alerts until D9 is decided, which is
   correct. `429`: Fabric throttling; the global call budget (`ORQENTIS_FABRIC_MAX_CONCURRENCY`, `_RATE_PER_SECOND`) slows
   every caller, check whether it is set too high. `5xx`/timeouts: a Fabric or Power BI outage.
3. Who it affects. `AppMetrics | where Name == 'orqentis.collector.errors' | summarize sum(Sum) by tostring(Properties.collector), bin(TimeGenerated, 15m)`.
   If *every* collector is failing the heartbeat alert follows within 15 minutes: treat the cause as one problem (usually
   credentials: section 9).

### Alert: collection heartbeat

**Meaning.** There was no successful collection (a collect + detect run that finished and in which not everything failed)
for `alertHeartbeatMinutes` (15). FabricOps is not looking at the estate, so a real failure would go unnoticed. It is the
alert for a dead scheduler, a lost lease, a hung job, an expired credential on every collector, and a broken metrics
pipeline. It is switched off by the template while `schedulerEnabled` or `collectEnabled` is false. Sev 1.

**First three things to check.**

1. Is the app up and leading? `curl -s https://<fqdn>/health/ready`: `"scheduler": "leader"` is right; `"standby"` with no
   other replica means the lease is held by a stale holder (section 12); `"disabled"` means someone turned the scheduler
   off (section 13); `503` is a build that is not completing. If *app down* or *restart loop* also fired, start there.
2. Is it the scheduler or the telemetry? `AppMetrics | where TimeGenerated > ago(30m) | where Name startswith 'orqentis.' | summarize latest = max(TimeGenerated)`.
   No rows at all but the console shows `telemetry ingested` lines (`ContainerAppConsoleLogs_CL | where Log_s has 'telemetry ingested'`)
   means collection works and the **metrics export is broken** (check the `APPLICATIONINSIGHTS_CONNECTION_STRING` secret and
   that the app can reach the Application Insights ingestion endpoint). Metrics present but only `failed` runs: see the
   collector errors alert and section 9.
3. No runs at all. Look for `scheduled job still running; slot skipped` (a hung collect job: the 30-minute job timeout
   cancels it; restart the revision if it does not), `scheduler leadership lost`, or `scheduler waiting before it takes the
   lease` (the start-up delay, 120 s by default, and up to 900 s). After a deployment the old revision may still hold the lease
   (section 12).

### Alert: audit verification failure

**Meaning.** The scheduled `audit-verify` job found the audit hash chain (or its immutable anchor, **after WS6**) invalid,
or could not read the log after retrying; or an append was refused after the log shrank. The audit trail is the record of
what the agent and the humans did, so **treat this as tampering until shown otherwise**. Sev 0.

**First three things to check.** Preserve evidence first; do not restart, redeploy or "fix" anything yet.

1. Read the failure. `ContainerAppConsoleLogs_CL | where Log_s has 'audit chain verification failed' | project TimeGenerated, Log_s | take 5`:
   `check` is `chain` (records altered, removed or reordered), `anchor` (the chain no longer matches its immutable anchor) or
   `read` (the log could not be read, which is an availability problem, not proof of tampering); `first_invalid_sequence`
   is the first record that does not verify; `reason` says why (`hash chain broken`, `sequence gap`, `log truncated`).
2. Take a read-only copy and verify it yourself:
   `az storage file download --account-name <storage account> --share-name orqentis-data --path audit/audit.jsonl --dest ./evidence/audit/audit.jsonl --auth-mode login --enable-file-backup-request-intent`
   (the storage account is the `storageAccountName` deployment output), then `orqentis audit --data-dir ./evidence`. Compare
   the records around `first_invalid_sequence` with any older copy you hold (section 8).
3. Who could have written it? The storage account's activity log (`az monitor activity-log list --resource-id <storage account id>`)
   and data-plane diagnostic logs if enabled; who holds `Storage File Data Privileged Contributor`; recent deployments.

Do not edit the log to make it verify. After the investigation the owner decides whether to archive the broken file and
start a new chain, which is a deliberate, audited act. With the chain valid again (or archived) the next hourly run closes
the alert. A `read` failure that repeats is a storage problem: check the Azure Files mount (section 5, *app down*).

## 6. On call: the first five minutes

1. Acknowledge the alert in the portal and note its severity: Sev 0 is integrity, Sev 1 means FabricOps is down or blind,
   Sev 2 means it is partly impaired.
2. Open the workbook for the last 4 hours and the section above for the alert. Do the three checks in order.
3. If a deployment is the likely cause and the first check does not clear it in 15 minutes, roll back (section 10) rather
   than debug live. Rolling back is safe: the digest was good before.
4. Tell the owner when FabricOps was blind: collection resumes from its watermarks, so events are not lost (collectors
   re-read up to `ORQENTIS_COLLECTION_MAX_LOOKBACK_HOURS`, 168 h, after downtime), but detection of an incident is late by
   the length of the gap.
5. Write down what happened in the environment's ticket. If an alert was a false alarm, change its threshold in the
   parameter file instead of muting it.

## 7. Test-firing the alerts

Done in `dev` only, by the person who deploys it, after the action group has a receiver. Each test is a *deliberate
failure* followed by a restore. Allow for ingestion delay (about 2-5 minutes) plus the evaluation frequency.

| Alert | Make it fire | Expect | Restore |
|---|---|---|---|
| App down | `az containerapp revision deactivate -g $RG -n ca-$PREFIX --revision <active revision>` | Alert after about 6 minutes; if nothing fires, `Replicas` reports no data when there is no replica (add an availability test) | `az containerapp revision activate ...` |
| Restart loop | `az containerapp revision restart -g $RG -n ca-$PREFIX --revision <revision>` three times, each after `/health/ready` answers | Alert at the next evaluation after the third start; the workbook's *Restarts* chart shows 3 | none |
| 5xx rate | Deploy `dev` with `fabricMode=rest` and no contracts file (the app stays up, builds in the background and answers `503 NOT_READY`), then send 25 authenticated `GET /incidents` calls within 10 minutes | Alert once 5 % of at least 20 requests are 5xx | Redeploy with the right parameters |
| Collector errors | Remove the reader identity's Viewer role from one sandbox workspace, or use the known 403 of `semantic-model-refreshes` | One alert per failing collector after two failed runs | Restore the role |
| Heartbeat | Deploy with `alertHeartbeatMinutes=5` and `schedulerStartupDelaySeconds=900` (no job runs for 15 min after a restart), then restart the revision | Alert about 10 minutes after the restart | Redeploy with the defaults |
| Audit verification | Download `audit/audit.jsonl`, change one character inside a record without changing its length, upload it, restart the revision (the job runs right after the start-up delay) | Sev 0 alert; `GET /audit/verify` reports `valid: false` | Upload the original file again |

For every test: check the notification reached the receiver, that it carries the runbook link, and that the alert closes
after the restore. Record the result in the production-readiness ledger.

## 8. Restore

* **Today (Azure Files + SQLite, before WS6).** The data share has soft delete (14 days) and no snapshots or backups:
  undeleting a share (`az storage share-rm restore`) is the only built-in recovery, and nothing is deployed in this mode
  to `test` or `prod` before WS6. Treat `dev` data as disposable (`orqentis demo` and the sandbox regenerate it).
* **After WS6.** PostgreSQL has point-in-time restore and evidence lives in immutable Blob storage; the restore runbook
  (exact commands to restore to a new server and repoint the app, and what happens to the audit chain) is WS6's
  `docs/operations/restore.md`, and this guide links to it once it merges. The audit chain's immutable anchor lets
  `audit-verify` prove that a restore did not drop or rewrite records.
* Whatever the store, a restored FabricOps resumes from its watermarks and cursor, re-reading up to
  `ORQENTIS_COLLECTION_MAX_LOOKBACK_HOURS`. Rehearse one restore per environment before relying on it.

## 9. Keys and secrets

FabricOps keeps no credential of its own in the repository, in parameters or in CI variables. What exists, and how to rotate it:

| Credential | Where it lives | Rotate by |
|---|---|---|
| Reader / actor managed identities | Entra ID (no secret) | Nothing to rotate. Re-grant Fabric workspace roles if an identity is recreated: its principal id changes |
| Fabric service principal secret (`fabricAuthMode=service_principal`) | Key Vault secret, referenced versionless by `fabricServicePrincipalSecretUri` | Create a new secret on the app registration (`az ad app credential reset --id <appId> --append`), set it as a **new version of the same Key Vault secret**, then `az containerapp revision restart` so the process reads it; delete the old credential once collection works. Do this before it expires |
| Fabric service principal certificate | Key Vault secret holding the PEM, mounted read-only | Same, with a new certificate version; restart the revision |
| Application Insights connection string | Container App secret (not a parameter) | It lets its holder write telemetry to the component, not read it. To change it, recreate the component and redeploy |
| Storage account key (the Azure Files mount) | Read at deployment into the environment's storage definition | `az storage account keys renew`, then **redeploy** so the environment picks up the new key; the share is unavailable until you do |
| Entra app registration of the API | Entra ID, no client secret (the API validates bearer tokens only) | Nothing to rotate; review the app role assignments quarterly |
| CI/CD identities (`fabricops-cicd-*`) | Entra ID federated credentials (OIDC), no secret | Nothing to rotate; keep the subjects as `docs/delivery.md` shows |
| Live-test service principal (`fabricops-live-reader`) | One client secret outside the repository, on the orchestrator's machine only; its expiry is recorded in `docs/live-environment.md` | `az ad app credential reset --id <appId>` before the date there, update the local copy, delete the old one. Never store it in CI |
| Teams Workflows webhook | The deployer's secret store (section 4) | Recreate the Workflow, redeploy with the new URL |

Rotate on a calendar the expiry dates make for you, not after an outage: a secret that expires makes every collector
fail, and the first symptom is the *collector errors* alert followed by the *heartbeat*.

## 10. Upgrade and roll back

Every environment runs an image **by digest**, built once and promoted `dev` then `test` then `prod`
(`delivery.md`). An upgrade is therefore a merge to `main` (CD deploys `dev`, then waits for the reviewers of `test` and
`prod`). To see what runs: `az containerapp show -g $RG -n ca-$PREFIX --query "properties.template.containers[0].image" -o tsv`.

**Roll back by redeploying a previous digest** (`delivery.md`, *Rolling back*; the digest is in the earlier CD run's
*Image* summary, its `image-digest` artifact, or `az acr manifest list-metadata -r <registry> -n orqentis-fabricops`):

```bash
export CONTAINER_IMAGE=<registry>/orqentis-fabricops@sha256:<previous digest>
export ACR_LOGIN_SERVER=<registry> ACR_RESOURCE_ID=<registry resource id> ENTRA_API_CLIENT_ID=<that environment's API app id>
az deployment group create -g "$RG" -p infra/bicep/env/prod.bicepparam      # or dev / test
```

Then confirm with `scripts/smoke-test.sh`'s checks: the latest revision is ready and serves all traffic, `/health` is `200`,
`/health/ready` is `200`. A deployment creates a new revision and the old one drains; the new process waits
`schedulerStartupDelaySeconds` (120) before it takes the lease or runs a job, so the two do not both write. Until WS6 the old
and the new revision still share one volume for those minutes: deploy in a quiet period and check `GET /audit/verify`
afterwards (the `audit-verify` job does too). Do not roll back across a change of the stored data format without reading the
release notes. Redeploying the same digest with other parameters is also how a threshold, a window or a receiver changes.

## 11. Data retention

| Data | Where | Retention | Change it |
|---|---|---|---|
| Console and system logs, application metrics, requests | Log Analytics workspace `log-$PREFIX` | `logRetentionInDays`: 30 (`dev`, `test`), 90 (`prod`), maximum 730 | `logRetentionInDays` in the parameter file; raising it costs money (`cost.md`) |
| Application Insights data | Same workspace (workspace-based component) | Same | Same |
| Incidents, approvals, events, evidence, audit chain **today** | Azure Files share `orqentis-data` (SQLite, evidence files, `audit/audit.jsonl`) | No expiry; share soft delete 14 days | `fileShareQuotaGiB`; copy with `az storage file download-batch` |
| Incidents, approvals, events, leases, audit chain **after WS6** | PostgreSQL Flexible Server | Point-in-time restore window (WS6 parameter, 7 days in the plan) | WS6's retention parameter |
| Evidence and reports **after WS6** | Blob storage with version-level immutability | Immutability period set by WS6; cannot be shortened | WS6 parameters |

Alert history and the action group's activity follow the Azure Monitor defaults. The audit trail and evidence are never
deleted by the application. A log retention shorter than the time you want to investigate back is a decision the owner
makes with `cost.md` in front of them.

## 12. Scaling and the leader lease

* **One replica today.** The Bicep pins `minReplicas = maxReplicas = 1`: SQLite, the hash-chained audit file and the in-process
  lease allow one writer. Do not scale out before WS6. `/health/ready` reports `"scheduler": "leader"`.
* **After WS6** two or more replicas are supported. They compete for a database lease; exactly one holds it and runs the
  jobs (`orqentis.scheduler.leader` is `1` on that replica, `0` on the others), the rest serve the API. The lease TTL is
  `schedulerLeaseTtlSeconds` (60; at least three times `schedulerTickSeconds`). If the leader dies, a standby takes over
  within about one TTL, with a higher fencing token, and the dead leader's late writes are rejected. Watch
  `orqentis.scheduler.lease.changes`: a few changes a day around deployments are normal; a steady stream is a flapping
  leader (an overloaded or network-partitioned replica).
* A `standby` replica with no leader anywhere means the lease holder is gone and its lease has not expired yet; wait one TTL.
  If it persists, restart the revision.

## 13. Pausing the scheduler safely

Pause to investigate, to freeze FabricOps during a Fabric maintenance window or before a risky deployment. The API keeps
working; nothing is collected, detected, investigated, swept or verified while it is paused.

1. Expect the *collection heartbeat* alert 15 minutes after the last run. The template switches that rule off when it is
   deployed with `schedulerEnabled=false` or `collectEnabled=false`, so **pause through a deployment**:
   `az deployment group create ... -p infra/bicep/env/<env>.bicepparam -p schedulerEnabled=false` (this also removes the
   heartbeat alert's noise). For a pause shorter than an evaluation, disable the rule instead:
   `az monitor scheduled-query update -g $RG -n $PREFIX-collection-heartbeat --disabled true`, and enable it afterwards.
2. Let running work finish or be cancelled: a deployment stops the scheduler, cancels its jobs and releases the lease
   before the process exits; investigations that were running return their incident to `DETECTED`.
3. To resume, redeploy with `schedulerEnabled=true`. The new process waits the start-up delay, takes the lease and
   collects from its watermarks, so nothing between is lost (up to the 168 h lookback).
4. To switch off one job only, use its `...Enabled` parameter (for example `autoInvestigateEnabled=false` to stop automatic
   investigations while people read incidents). A paused `audit-verify` or `collect` job should not stay paused: log it in
   the ticket and set a date to resume.

Do not stop FabricOps by deleting the Container App or its volume; stop the scheduler and leave the data.

## 14. Known gaps

* There is no endpoint that lists per-job scheduler state; use the workbook and the `orqentis.scheduler.*` metrics
  (a read-only `GET /scheduler` is a candidate follow-up).
* The app-down alert depends on the `Replicas` platform metric; an availability test for publicly reachable deployments is a
  follow-up.
* CD does not supply the alert receivers (section 4).
* The KQL of the alerts is exercised by the dev test fire, not by an offline test (ADR-0019, *Consequences*).
