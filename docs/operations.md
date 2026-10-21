# Operations guide

How to run a deployed Orqentis FabricOps: what to watch, what each alert means and what to do first, how to restore,
rotate, upgrade, roll back and pause it. It is written for the on-call engineer and the platform owner of an
environment (`dev`, `test` or `prod`), and it describes what the repository deploys: PostgreSQL and Blob storage, two
or more replicas in `test` and `prod`. The design of the metrics and alerts is
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
| `orqentis.collector.runs` | counter | `collector`, `outcome` = `ok`/`error`/`unavailable` | `error` when the collector, or any of its scopes, raised a Fabric error (the *collector errors* alert reads only this); `unavailable` when it could not observe by design and nothing else failed. An unavailable collector still records a run and its duration |
| `orqentis.collector.errors` | counter | `collector` | Failed collector runs or failed scopes (one per failing pipeline or model) |
| `orqentis.collector.backlog` | gauge | `collector` | Units the collector's last run left over because of a per-run cap, summed over its scopes: for `delta-schema-changes`, Delta commits not yet read. 0 when caught up; a number that keeps growing means a table commits faster than `ORQENTIS_DELTA_LOG_MAX_COMMITS` per collection cycle |
| `orqentis.collector.unavailable` | counter | `collector` | A collector (or scope) that could not observe **by design**: a permission gap the deployment documents (`ORQENTIS_EXPECTED_FORBIDDEN_COLLECTORS`) or a capability the platform does not expose. Not an error and never alerts; `/health/ready` lists the collector names under `unavailable` |
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
| `orqentis.notifications` | counter | `channel` = `teams_workflows`/`webhook`, `outcome` = `sent`/`failed`/`dropped`/`abandoned` | How each notification ended (retries are attempts, not outcomes): `failed` is a refusal or exhausted attempts, `dropped` an outbox that was full, `abandoned` a shutdown |
| `orqentis.audit.verifications` | counter | `result` = `valid`/`invalid`/`error` | Scheduled audit verifications |
| `orqentis.audit.anchor.age` | gauge (s) | none | Age of the newest immutable audit anchor as the last scheduled verification saw it; absent when none exists. Alert when it exceeds twice `schedulerAuditAnchorIntervalMinutes` (the anchor job is not working) |
| `orqentis.contracts.reload.failures` | counter | `error_type` | Refreshes in which the estate contracts file was refused or missing (the previous contracts stay in force). One per failing refresh, so a persisting failure is a rising count |

**What a collection outcome means.** `success`: every collector and scope worked. `degraded`: some failed, at least one
worked (FabricOps sees part of the estate); the heartbeat still advances, and the *collector errors* alert is what
tells you. `failed`: everything that was tried failed, or the run raised (an expired credential or a Fabric outage looks
like this); the heartbeat does **not** advance. `cancelled`: a deploy, a lost lease or the job timeout cut the run short.

## 4. Alert receivers

Who the alerts notify is an **action group**. There are two ways to give the alerts one, and the difference matters in a
public repository: GitHub Actions prints each step's environment in the job log, so a value that reaches the pipeline as
a variable, such as an email address, is published to the world.

### Preferred: an action group you manage yourself (`ALERT_ACTION_GROUP_ID`)

The pipeline carries only the group's *identifier*; the people in it never pass through CI. Create the group once, outside
the pipeline, and tell the environment which one it is:

```bash
# 1. Create it in the environment's resource group (a different name than the template's own ag-$PREFIX).
az monitor action-group create -g "$RG" -n "ag-$PREFIX-oncall" --short-name fabricops \
  --action email oncall oncall@example.com usecommonalertschema        # your team's address; repeat --action for more
# 2. Give the pipeline its resource id: an identifier, not a receiver. This is the only thing that goes through CI.
GROUP_ID=$(az monitor action-group show -g "$RG" -n "ag-$PREFIX-oncall" --query id -o tsv)
gh variable set ALERT_ACTION_GROUP_ID --repo "$REPO" --env dev --body "$GROUP_ID"      # one per environment
```

From the next deployment every alert uses that group and the template **creates and changes no action group**; add or
remove people with `az monitor action-group update` or the portal, never through the pipeline. A Teams Workflows webhook
is added the same way (`--add-action webhook teams <url> usecommonalertschema`, or the portal): the URL is a secret, so
type it where it is not recorded (not into a shared shell history), and remember that the action group stores it in clear
and shows it to anyone who can read the group, so keep Reader on the resource group to the operators.

* The deployment stops, with a message, if the value is not an action group's resource id, or if it is combined with
  `ALERT_EMAIL_RECEIVERS` or `ALERT_TEAMS_WEBHOOK_URI` (receivers next to a managed group would otherwise be ignored
  without a word).
* The deploying identity needs **read** access to the group (Contributor on the environment's resource group covers a group
  in it; give Reader on the group when it lives in another resource group or subscription).
* An `ag-$PREFIX` created by earlier deployments stays where it is (a deployment never deletes). Either make it the managed
  group (set `ALERT_ACTION_GROUP_ID` to its id and keep its receivers by hand from then on: the template no longer touches
  it, so they survive) or delete it.
* The pipeline cannot see whether the group has a receiver. Look after the first deployment:
  `az monitor action-group show -g $RG -n <name> --query "{email:emailReceivers[].emailAddress, webhook:webhookReceivers[].name}"`,
  and press **Test action group** in the portal. The deployment output `alertActionGroupId` names the group the alerts use.

### Only for a private repository or a local shell: receivers as deployment inputs

The template can create the group itself from two inputs. They are never stored in the repository or in a parameter file,
and they must **never be GitHub variables of a public repository**:

| Input | Where it is read | Form |
|---|---|---|
| `ALERT_EMAIL_RECEIVERS` | environment of whoever runs the deployment, read by `infra/bicep/env/*.bicepparam` | comma separated addresses (spaces are trimmed) |
| `ALERT_TEAMS_WEBHOOK_URI` | same | a Microsoft Teams **Workflows** webhook URL ("When a Teams webhook request is received") |

```bash
export ALERT_EMAIL_RECEIVERS="oncall@example.com,platform@example.com"   # your team's addresses
export ALERT_TEAMS_WEBHOOK_URI=...                                         # from your secret store, never typed into a file
az deployment group create -g "$RG" -p infra/bicep/env/dev.bicepparam      # with the CD variables of delivery.md exported
```

* The webhook URL carries its own signature: keep it in the deploying person's secret store, rotate it by recreating the
  Workflow and redeploying.
* The template owns this group, so a deployment without the inputs leaves it without receivers and removes any added by
  hand. That is another reason to prefer the managed group above for anything the pipeline deploys.
* The deployment output `alertEmailReceiverCount` says how many email receivers the last deployment set (0 means alerts
  notify nobody by email).

### Either way

* The group sends the common alert schema. A Teams Workflow shows a chat message from it only if its flow reads
  `data.essentials.alertRule`, `data.essentials.severity` and the `runbook` custom property and posts them; build the
  flow once and test it with **Test action group** in the portal.

## 5. The alerts

Six rules, all Sev 0-2, all in the resource group, all using the action group. Log alerts are evaluated every
`alertEvaluationFrequencyMinutes` (5) over a window; they are stateful (`autoMitigate`): a rule opens one alert and resolves it
when the condition clears, so you are not re-notified at every evaluation. Every rule's description and notification carries a link to its
section below. **Triage order when several fire:** audit verification, then app down, then restart loop, then heartbeat,
then collector errors, then 5xx: the earlier ones usually explain the later ones.

| Alert (`$PREFIX-...`) | Sev | Fires when (defaults) | Parameters |
|---|---|---|---|
| `audit-verification-failure` | 0 | a scheduled audit verification failed or could not read the log | `alertAuditFailureWindowMinutes` 0 (derived: 120 min at the hourly default) |
| `app-down` | 1 | no running replica for 5 min | `alertAppDownWindowMinutes` 5 |
| `restart-loop` | 1 | 3 process starts or container terminations in 15 min | `alertRestartWindowMinutes` 15, `alertRestartThreshold` 3 |
| `collection-heartbeat` | 1 | no successful collection for 15 min | `alertHeartbeatMinutes` 15 |
| `collector-errors` | 2 | one collector had 2 failing runs in 15 min (one alert per collector) | `alertCollectorErrorWindowMinutes` 15, `alertCollectorErrorRuns` 2 |
| `5xx-rate` | 2 | 5 % or more of at least 20 requests in 10 min answered 5xx | `alertHttp5xxWindowMinutes` 10, `alertHttp5xxRatePercent` 5, `alertHttp5xxMinRequests` 20 |

Windows are limited to the durations Azure accepts for a log alert (5, 10, 15, 30, 45, 60, 120, 180, 240, 300, 360, 1440 or
2880 minutes; 1, 5, 15, 30 or 60 for the app-down metric alert), so a value such as 20 or 65 is rejected when the parameter
is read. A window must also be at least as long as the evaluation frequency: otherwise the deployment stops with a message
naming the windows that are too short.
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
*heartbeat* and *5xx* alerts. If `Replicas` reports nothing (instead of `0`) for an app with no active revision, this alert
cannot fire: **the *collection heartbeat* is the backstop**, firing within `alertHeartbeatMinutes` (15) because nothing
collects any more. The dev test fire (section 7) shows which of the two happens; an availability test is the follow-up
that would make this alert independent of that metric.

### Alert: restart loop

**Meaning.** The app process started, or its container was terminated, at least `alertRestartThreshold` (3) times in the
window (15 min). One deployment is one start, so three in a quarter of an hour is a crash loop, not a rollout. The count is
the larger of two independent signals: the application's own `orqentis process started` console line (it survives a
metrics outage and a crash before the first metric is exported) and the platform's termination events in the system log.
The console line is recognised by its JSON fields (`logger` = `orqentis.runtime.service`, `message` = `orqentis process
started`), not by the phrase appearing anywhere in a line, so an error message from Fabric that quotes it does not count.
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
   expired, a Fabric tenant setting changed). **A permanent `403` from `semantic-model-refreshes` is by design**: refresh history
   needs dataset Write, which the reader never has (programme decision D9). Refresh outcomes come from workspace monitoring
   (`ORQENTIS_REFRESH_SOURCE`, default in rest mode), so that call is no longer made; a deployment that turns
   `powerbi_api` on lists the collector in `ORQENTIS_EXPECTED_FORBIDDEN_COLLECTORS` and the 403 becomes *capability
   unavailable* (section 15), not an error. `429`: Fabric throttling; the global call budget (`ORQENTIS_FABRIC_MAX_CONCURRENCY`, `_RATE_PER_SECOND`) slows
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

**Meaning.** The scheduled `audit-verify` job found the audit hash chain, one of its immutable anchors or the epoch marker invalid,
or could not read the log after retrying; or the anchor job refused to anchor a head that contradicts what was
anchored (the same log message). The audit trail is the record of
what the agent and the humans did, so **treat this as tampering until shown otherwise**. Sev 0. The log leg matches the
exact JSON fields `logger` and `message` of our own two log lines, so text chosen by Fabric or the model cannot raise it.

**It does not resolve itself.** After a restart the process forgets that the log had shrunk, so the next verification
could look clean while the evidence is gone; a person resolves this alert after the investigation. While the failure
persists the action group is reminded at most every six hours (`muteActionsDuration`), not at every evaluation.

**First three things to check.** Preserve evidence first; do not restart, redeploy or "fix" anything yet.

1. Read the failure. `ContainerAppConsoleLogs_CL | where Log_s has 'audit chain verification failed' | project TimeGenerated, Log_s | take 5`:
   `check` is `chain` (records altered, removed or reordered), `anchor` (the chain contradicts an immutable anchor or the
   epoch marker), `anchor-continuity` (the anchor job refused to anchor a head that contradicts the previous anchor) or
   `read` (the log could not be read, which is an availability problem, not proof of tampering); `first_invalid_sequence`
   is the first record that does not verify; `reason` says why (`hash chain broken`, `sequence gap`, `indexed columns
   disagree`, `contradicts the audit anchor (epoch N) for sequence S`, `epoch counter was moved`, `restored or rewound`).
2. Verify it yourself from a host with access to the database, without restarting anything: `orqentis audit` (the runbook's
   section 6, [operations/restore.md](operations/restore.md), has the variables and the roles). Compare the records around
   `first_invalid_sequence` with a point-in-time copy of the database (restore to a new server; it never touches the live
   one) and with the anchors in the `audit-anchors` container, which nobody can change before the retention ends.
3. Who could have written it? PostgreSQL's server logs and the storage account's activity log
   (`az monitor activity-log list --resource-id <storage account id>`); who holds `Storage Blob Data Contributor` on
   `audit-anchors` and on `audit-epochs`; who is in the PostgreSQL administrator group; recent deployments and restores
   (a restore without `orqentis audit start-epoch` is reported as `restored or rewound` and is not tampering).

Do not edit the log to make it verify. After the investigation, if records were lost by a restore, an audit operator records
it with `orqentis audit start-epoch` (it verifies the chain first and refuses over tampering unless the operator gives an
audited override reason): a deliberate, audited act. With the chain valid again, resolve the alert in the portal; the next
hourly run confirms it. A `read` failure that repeats is a database problem: check connectivity, the database role and the
Entra token (section 5, *app down*). A verification that is `valid` but reports an old `last_anchor_age_seconds`, or
`/health/ready` saying `degraded: audit anchoring stale`, means the anchor job is not working (a missing storage role or a
stopped leader): the anchors stop protecting new records until it works again.

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
| Audit verification | **Never on the live chain.** Restore the database to a scratch server ([operations/restore.md](operations/restore.md)), edit one record there as a superuser (`ALTER TABLE audit_records DISABLE TRIGGER USER; UPDATE ...`) and point a scratch deployment at it, or run `orqentis audit` against it | `hash chain broken` (and, with the scratch deployment, the Sev 0 alert) | Delete the scratch server |

For every test: check the notification reached the receiver, that it carries the runbook link, and that the alert closes
after the restore. Record the result in the production-readiness ledger.

## 8. Restore

* **PostgreSQL** has point-in-time restore (7 days in `dev` and `test`, 35 in `prod`, `postgresBackupRetentionDays`) and
  evidence lives in immutable Blob storage. The restore runbook (exact commands to restore to a new server and repoint the
  app, what happens to evidence, and the audit epoch an operator must open) is
  [operations/restore.md](operations/restore.md), with a rehearsal checklist the owner signs off. The audit chain's immutable
  anchors let `audit-verify` prove that a restore did not drop or rewrite records it should have kept.
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
| Storage account key | Does not exist: shared-key access is disabled on the evidence account and the app uses its managed identity | Nothing to rotate |
| PostgreSQL password | Does not exist: Microsoft Entra authentication only, the access token is the password | Nothing to rotate |
| Entra app registration of the API | Entra ID, no client secret (the API validates bearer tokens only) | Nothing to rotate; review the app role assignments quarterly |
| CI/CD identities (`fabricops-cicd-*`) | Entra ID federated credentials (OIDC), no secret | Nothing to rotate; keep the subjects as `docs/delivery.md` shows |
| Live-test service principal (`fabricops-live-reader`) | One client secret outside the repository, on the orchestrator's machine only; its expiry is recorded in `docs/live-environment.md` | `az ad app credential reset --id <appId>` before the date there, update the local copy, delete the old one. Never store it in CI |
| Teams Workflows webhook | The managed action group (section 4), or the deployer's secret store for a template-managed group | Recreate the Workflow; update the managed group (`az monitor action-group update`) or redeploy with the new URL |

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
`/health/ready` is `200`. A deployment creates a new revision and the old one drains. The two revisions overlap safely: one audit chain
(the head row is locked by every append), one scheduler leader (the database lease, fenced writes), at most one execution per
approval, and schema migrations that stay compatible with the previous release. Check `GET /audit/verify` afterwards (the
`audit-verify` job does too). Do not roll back across a change of the stored data format without reading the
release notes. Redeploying the same digest with other parameters is also how a threshold, a window or a receiver changes.

## 11. Data retention

| Data | Where | Retention | Change it |
|---|---|---|---|
| Console and system logs, application metrics, requests | Log Analytics workspace `log-$PREFIX` | `logRetentionInDays`: 30 (`dev`, `test`), 90 (`prod`), maximum 730 | `logRetentionInDays` in the parameter file; raising it costs money (`cost.md`) |
| Application Insights data | Same workspace (workspace-based component) | Same | Same |
| Incidents, approvals, events, leases, jobs, audit chain | PostgreSQL Flexible Server | No expiry in the database; point-in-time restore window 7 / 7 / 35 days (`dev` / `test` / `prod`) | `postgresBackupRetentionDays` |
| Evidence, reports, audit anchors, epoch markers, the contracts file | Blob storage with versioning, soft delete and version-level immutability | `evidenceRetentionDays` (7 / 30 / 365); cannot be shortened once the policy is locked (`EVIDENCE_IMMUTABILITY_LOCKED`) | `evidenceRetentionDays`, `evidenceSoftDeleteDays` |

Alert history and the action group's activity follow the Azure Monitor defaults. The audit trail and evidence are never
deleted by the application. A log retention shorter than the time you want to investigate back is a decision the owner
makes with `cost.md` in front of them.

## 12. Scaling and the leader lease

* **Replicas.** `minReplicas` and `maxReplicas` are 1 in `dev` and 2 in `test` and `prod`; the PostgreSQL storage makes two or
  more safe. They compete for a database lease; exactly one holds it and runs the
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
* Nothing checks that the action group named by `ALERT_ACTION_GROUP_ID` has a receiver, or that it exists before the first
  deployment (the deployment of the alert rules fails if it does not): look at it once (section 4).
* The KQL of the alerts is exercised by the dev test fire, not by an offline test (ADR-0019, *Consequences*).

## 15. Collectors, what each needs, and "capability unavailable"

| Collector (`collector` dimension) | Reads | Minimum grant | Scope |
|---|---|---|---|
| `pipeline-runs` | pipeline job instances and activity runs | workspace Viewer | one per pipeline |
| `notebook-runs`, `spark-job-runs`, `dataflow-runs` | job instances of those items | workspace Viewer | one per item |
| `semantic-model-refreshes` | Power BI refresh history (`ORQENTIS_REFRESH_SOURCE` = `powerbi_api` or `both`) | dataset Write (the reader never has it) | one per model |
| `workspace-monitoring-refreshes` | the monitoring KQL database, recognised per workspace by its documented name (legacy `Monitoring KQL database`, or `<workspace> <item>_Database`; `ORQENTIS_WORKSPACE_MONITORING_DATABASE` overrides) (default refresh source in rest mode) | to be confirmed live: see `docs/security.md` section 11 | one per workspace that holds a semantic model; one with no monitoring database in the estate is reported as capability unavailable, without an API call |
| `delta-schema-changes` | `_delta_log` of the contracts' `delta_tables` (at most `ORQENTIS_DELTA_LOG_MAX_COMMITS` commits per table per run, oldest first, with `orqentis.collector.backlog` showing what waits; the schema in force is kept as a `SCHEMA_SNAPSHOT` event) | a OneLake data access role with Read on exactly those tables | one per declared table |
| `capacity-metrics` | the Capacity Metrics app's model (configured by workspace and model id) | Read and Build on the model (open decision) | one per capacity |

**Capability unavailable is not an error.** A collector (or one scope) that cannot observe by design (a capability the platform
does not expose, a workspace without a monitoring database, or an HTTP 403 the deployment lists in
`ORQENTIS_EXPECTED_FORBIDDEN_COLLECTORS`) is counted in `orqentis.collector.unavailable{collector}`, listed by name under
`unavailable` in the `GET /health/ready` body, and reported per scope in the `collector_unavailable` field of
`POST /telemetry/collect`. It does not count as a failed run, never raises the *collector errors* alert and does not stop the
heartbeat. It also means FabricOps is blind to that signal, so a name that stays in the list needs an owner. Any other 401 or 403
remains a collector error.

**An SLA breach is not declared from blindness.** If the collector that observes an SLA's freshness item has not completed a
pass for it within `ORQENTIS_SLA_MAX_TELEMETRY_LAG_MINUTES` (30 in rest mode), the sweep withholds the breach and logs
`sla breach withheld`; the breach is recorded on the first sweep after telemetry is current. If breaches seem missing, check the
collector errors first.
