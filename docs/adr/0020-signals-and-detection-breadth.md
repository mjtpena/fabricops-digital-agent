# ADR-0020: Signal sources and detection breadth: job rules, workspace monitoring, SLA breaches, Delta schema changes and capacity

* Status: Accepted (production programme, WS7). Implements decision D9 and the schema-change half of
  [ADR-0009](0009-capacity-and-schema-signals.md); the capacity half of ADR-0009 is built but **not confirmed** (see 6).
* Date: 2026-10-21

## Context

Detection had one rule (a pipeline run failed) and one workflow. The first live end-to-end run
(`docs/live-environment.md`, "Live end-to-end run 1") showed what that missed: a semantic model refresh failed and nothing
was recorded, because the Power BI refresh history needs dataset **Write** and the reader identity must never have a write
role; an SLA breach was assessed and recorded and never became an incident; a notebook that raised a generic exception
ended as `UNKNOWN`; the schema-drift report named the changed column `'unknown'`; and no producer existed for Delta schema
changes or for capacity utilisation. Notebook, Spark job definition and Dataflow runs were not collected at all.

The goal of this workstream: every injected failure class (schema drift, notebook failure, pipeline timeout, auth failure,
semantic model refresh failure, SLA breach) is detected by a deterministic rule within one collection interval and
investigated to the right root-cause category, or to an honest LOW-confidence "undetermined", using only read access with
proven minimum grants.

## Decision

### 1. Job collectors, one per item type

`ItemJobCollector` reads the finished job instances of notebooks, Spark job definitions and Dataflows and writes
`NOTEBOOK_RUN_*`, `SPARK_JOB_*` and `DATAFLOW_REFRESH_*` events (distinct types keep each rule, summary and metric honest).
It uses the same per-item watermarks as the pipeline collector and the same filter-after-reading listing. **There is no early
stop** when paging job instances: ADR-0013 section 8 stands (Fabric documents no order; a long run that started before the
watermark can finish after it). The brief assumed one existed; it never did. Items of the two new types are typed
(`SparkJobDefinition`, `Dataflow`); `PipelineRun` keeps representing any job instance, and Fabric job types map to items
case-insensitively (`RunNotebook`, `sparkjob`, `Refresh`).

### 2. Six deterministic rules, and one incident per failure

`DEFAULT_RULES` gains `NotebookFailureRule`, `SparkJobFailureRule`, `DataflowFailureRule`, `RefreshFailureRule` and
`SlaBreachRule`. The LLM still never opens an incident. The category of a job incident starts as `ACTIVITY_FAILURE`; the
investigation sets the final one.

A notebook that a pipeline ran fails *because* it failed, and the pipeline's incident already names it as the failure point.
Two incidents for one failure would page twice, so a candidate can carry `covered_by_activity_target`: the engine drops it
when a failed pipeline activity that targets the same item was recorded within `COVERAGE_WINDOW` (10 minutes) of the failure.
The match is the activity's target item id and time, never a name. Pipelines are collected first, so within a cycle their
activity failures are already stored when detection reads the job failure. Independently, a job whose `invokeType` is not
`Manual`/`Scheduled` is marked `launched_by_item` and opens nothing. **Residual race:** if the notebook is read as failed
while its pipeline is still `InProgress` (seconds, about 1 to 2% of cycles), the notebook incident opens first and the
pipeline's follows next cycle. The live game day should confirm whether Fabric lists pipeline-started runs in the
notebook's own job instances and what `invokeType` they carry; the dedupe is written not to depend on the answer.

### 3. Semantic model refresh failures (D9): workspace monitoring

The reader never gets write, so the Power BI refresh history is not the default source. `ORQENTIS_REFRESH_SOURCE`
(`workspace_monitoring` | `powerbi_api` | `both` | `none`) chooses; rest mode defaults to `workspace_monitoring`, mock mode
to `powerbi_api` (the synthetic scenarios replay a Power BI shaped history).

`RestMonitoringService` (behind the new `MonitoringService` port on `FabricClient`) finds a workspace's monitoring KQL
database over the Fabric API, then POSTs one **static, parameterised** KQL query to its query endpoint. Nothing is formatted
into the query: ids and times travel as `properties.Parameters`, the options set `request_readonly`, the result and the window
are bounded. The bearer token for the query endpoint is sent only to hosts inside the Fabric API's parent domain
(`*.fabric.microsoft.com`), because the endpoint is named by the response of an API a workspace member influences. Everything
returned is untrusted: control, bidi and zero-width characters are removed, Power BI privacy markers stripped, lengths
capped, ids must be GUIDs. `MonitoringRefreshCollector` has one scope per workspace that has a monitoring database in the
catalog (a workspace without one costs no call) and reads 30 minutes further back than its window, because events reach the
database minutes late (a refresh that just ended can look unfinished; the event store de-duplicates by refresh id and status).

**The spike could not settle permissions, and the documentation contradicts itself** (`docs/spikes/workspace-monitoring.md`):
the workspace monitoring overview says "Workspace users with at least the Contributor role can query the monitoring
database", while the Fabric roles page and the Kusto RBAC page describe workspace Viewer as read access to every item,
including KQL databases. Contributor is Admin on every KQL database of the workspace and is a write role, which D9 forbids.
If a Viewer-only principal is refused, the narrower options are item-level sharing of the monitoring database (Read), or a
database viewer entry for the principal, both listed in the spike; **the owner decides, not this change**. Until the live
check, a 403 from the monitoring query is a collector error unless the deployment declares it expected
(`ORQENTIS_EXPECTED_FORBIDDEN_COLLECTORS=workspace-monitoring-refreshes`).

**Both routes of a refresh failure reach the same category and runbook.** A refresh that fails inside a pipeline's refresh
activity is the pipeline's incident: the new `SIG-MODEL-REFRESH` signature classifies the engine's error as
`SEMANTIC_MODEL_REFRESH_FAILURE`, the runbook search includes the semantic model among the failure points, and the runbook's
action targets the failure point (the model), not the incident's primary item. The monitoring record of the same refresh
failure is covered by that incident (section 2). A refresh failure with no pipeline behind it opens the model's own incident
(`DET-REFRESH-FAILED`), investigated by `RefreshFailureWorkflow`, which also looks for an incident open at or upstream of the
model when it failed.

### 4. SLA breaches

`SlaBreachRule` opens one incident per SLA and deadline day (`sla:<sla id>:<date>`, as the sweep's events already carry).
Severity: the data product owner's declaration (`metadata.criticality`: critical, high, medium, low) wins, otherwise the
environment of the item that refreshes it (PROD CRITICAL, TEST MEDIUM, DEV LOW, unknown MEDIUM).

**A breach is not declared from blindness** (ADR-0013, open question 3). Before it records a `BREACHED` event the sweep checks
that the collector observing the freshness item completed a pass for it within `ORQENTIS_SLA_MAX_TELEMETRY_LAG_MINUTES`
(30 in rest mode, off in mock mode); otherwise the event is withheld and logged, and appears when telemetry is current. An
SLA incident is about a missed deadline, not about a failing item, so it is **not counted as a blocker** of the refresh it
reports on (that made every later sweep list the SLA incident itself as the blocking incident).

`SlaBreachWorkflow` asks why the freshness item did not finish: `LATE` (it did, after the deadline; start time and duration
compared with its earlier runs), `BLOCKED` (an incident on it or upstream of it was open), `FAILED` (its last run failed and
nothing is open) or `NOT_RUN`. The root-cause category is always `SLA_BREACH`; the new runbook `RB-SLA-BREACH-001` proposes
only a manual notification, so no approval is requested. The deterministic SLA evaluation is the "signature" signal of the
confidence model (the category is certain; the why is what the other signals measure).

### 5. Schema changes from the Delta log

Contracts gain `delta_tables`: the Delta tables to watch (lakehouse item id, table, optional schema), validated like every
other section (duplicate ids, the lakehouse exists and is a lakehouse, names cannot carry a path) and in the JSON schema.
`RestDeltaLogService` (the new `DeltaLogService` port) lists a table's `_delta_log` and reads the first bytes of a commit file
through the OneLake ADLS Gen2 API, read-only (`GET` only) with a `Range` header and a byte cap, keeping only the `metaData`,
`commitInfo` and `protocol` lines. `SchemaChangeCollector` reads the commits written since the watermark, finds the schema in
force before the first of them (reading back a bounded number of commits), and compares each schema with the one before it.
Columns added, removed or whose type changed are reported; a **rename is reported only when Delta column mapping proves it**
(same physical column, new logical name), otherwise it is one removed plus one added column, which the reasoner calls
"consistent with a rename". Events are `SCHEMA_CHANGED` with the same payload keys the investigation already consumed.

**Everything in a Delta log is untrusted** (table and column names, `commitInfo.operation`, `engineInfo`, `userMetadata`):
control characters, bidi characters and envelope-like tags are removed, lengths are clipped, the number of columns, lines and
bytes is bounded, a malformed line is skipped. The values are then scanned by the evidence recorder's instruction scanner like
every other Fabric string, flagged on the evidence, and told to the model inside the untrusted-data envelope.

The live "column `'unknown'`" finding was Spark's own message form (``A column, variable, or function parameter with name
`customer_id` cannot be resolved. Did you mean one of the following? [...]``, nested inside Fabric's JSON): the schema
signature now extracts the column and the suggested replacements, so the report names the changed columns even without a
Delta log, and with one it names the version and the rename.

### 6. Capacity (not confirmed)

`CapacityMetricsReader` implements `CapacityService.get_capacity_metrics` through `executeQueries` against the Capacity
Metrics app's semantic model, folding its 30-second timepoints into 10-minute buckets, with identifier validation (the DAX
has no parameters, so the capacity id must be a GUID and timestamps are built from integers) and a schema guard that fails
closed with `CapacityMetricsSchemaMismatch`. It is configured by `ORQENTIS_CAPACITY_METRICS_WORKSPACE_ID` and
`..._DATASET_ID` (both or neither). **ADR-0009 said a spike would confirm this source. It could not, offline, and the findings
count against it** (`docs/spikes/capacity-metrics.md`): Microsoft states the app's model is not a supported surface and two
app versions already name their tables differently; `executeQueries` needs **Build** on the model, which a workspace Viewer
does not have, cannot be granted to a service principal through the users API, and comes with Contributor (a write role).
The options (a security group with `ReadExplore`, a dedicated identity scoped only to the app workspace, or ADR-0009's own
fallback of Real-Time hub capacity events in an Eventhouse) are the owner's to choose; the adapter stays dormant until the
two ids are set. The Capacity Metrics app must not sit on the capacity it measures.

### 7. Workflows, evidence and reasoning

The 20 steps of `PipelineFailureWorkflow` are now a base class, `DiagnosticWorkflow`; steps 2 to 4 (what is observed about
the failure) are the only part a workflow supplies, and everything from item metadata to the report is shared. Pipeline
behaviour, step names and numbering are unchanged. New workflows: `ItemJobFailureWorkflow`, `RefreshFailureWorkflow`,
`SlaBreachWorkflow`; the SLA workflow comes first in the registry because it claims an incident by its rule, whatever kind of
item it is about. New evidence types `JOB_FAILURE` and `RUN_HISTORY`; new read tools `get_job_instance` and `get_job_instances`
(not on the agent's allowlist: the deterministic workflow calls them); the refresh history tool reads workspace monitoring
when that is the source. How an item has been running (a streak of failures, the last success) is context, not a cause that
preceded the failure, so it never counts as causal evidence.

The rule-based reasoner gains item-aware wording and four hypotheses (notebook exception, refresh failure, SLA breach) plus
findings for run history and for incidents already open upstream. Confidence stays computed in code: a generic notebook
exception with one source and no history is **LOW** (0.42), a refresh failure with an explicit table name LOW, a late run
MEDIUM, an SLA breach blocked by a known open incident HIGH. New runbooks: `RB-NOTEBOOK-FAILURE-001` (an exception raised by
the job's own code or data; read-only actions) and `RB-SLA-BREACH-001`; `RB-SM-REFRESH-FAILURE-001` is extended.

New signatures: `SIG-NOTEBOOK-EXCEPTION` (priority 30, category `ACTIVITY_FAILURE`; it matches Python and Spark traceback shapes
and Fabric's wrapper and never outranks a specific cause), `SIG-MODEL-REFRESH` (65), and the engine's "credentials ... are
invalid" wording joins the authentication signature. Redaction treated any key containing `signature` as a secret, so the
error evidence lost its own `signature_match`; the exact keys `signature_match`, `signature_id`, `primary_signature` and
`unconfirmed_signature` are now safe, by exact name (not by relaxing the pattern), and their values are still scanned.

### 8. Known permission gaps are not collector errors (ledger R13)

A collector (or one scope of it) that cannot observe by design is *capability unavailable*, not an error: always for
`FabricCapabilityUnavailableError`, and for an HTTP **403** only for collectors the deployment lists in
`ORQENTIS_EXPECTED_FORBIDDEN_COLLECTORS` (validated against the real collector names, so a typo cannot silently hide an
error). The ingestor records it (`IngestionResult.unavailable`), counts `orqentis.collector.unavailable`, keeps the scope's
watermark where it was, does not count it as a failed unit, and `/health/ready` lists the collector names under
`unavailable` (names only, never a scope or a message: the probe is unauthenticated). A 401, or a 403 nobody expects, stays an
error and keeps the collector-errors alert meaningful. With workspace monitoring the default source, the Power BI refresh
history call is no longer made at all in rest mode.

## Alternatives considered

* **Grant the reader Contributor to read refresh history or the monitoring database.** Rejected by D9: a write role on every
  item of the workspace for a monitoring read.
* **The admin API for refresh history.** A fallback only, with the owner's further approval; not built.
* **One generic "job failed" rule.** Rejected: per-type events and rules keep summaries, metrics and the SLA freshness lookup
  precise at the cost of a few enum members.
* **Merging a notebook incident into its pipeline's incident afterwards.** There is no incident merge, and the coverage test at
  open time is simpler and deterministic; the residual race is documented instead.
* **Reading Delta checkpoints.** They are Parquet; reading them needs a Parquet reader. Bounded JSON commits are enough while a
  checkpoint has not retired the commits we need, and a table whose log has none we can read reports nothing rather than
  guessing.
* **A "newest-first early stop" for job instances.** Unsound for the reason in ADR-0013 section 8; not added.
* **Treating a 403 as unavailable by default.** It would hide real permission regressions. It is opt-in and per collector.

## Consequences

* A new `FabricClient` port surface (`monitoring`, `delta_logs`), two adapters, three collectors, five rules and three
  workflows; the shared workflow base keeps the next workflow small.
* The reader identity's grants stay: workspace Viewer, and a OneLake data access role with Read on exactly the declared tables.
  **No write role is added anywhere.** Workspace monitoring must be enabled by a workspace admin (not by FabricOps) in every
  workspace whose refreshes are to be observed, and may need an item-level share for the reader (open question below).
* Two live confirmations gate the value of this change: whether Viewer can query the monitoring database, and what access
  the Capacity Metrics model needs. Both are in the PR's open questions, with exact checklists in the spike documents.
* Event volume rises: one event per finished notebook/Spark/Dataflow run and per refresh, and capacity buckets when configured.
  Collection reads one extra job-instance listing per such item per cycle; the global call budget applies.
* The contracts schema gained a section; `schemas/contracts.schema.json` is regenerated and a test pins it.
