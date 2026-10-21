# Changelog

All notable changes to Orqentis FabricOps are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Cutting a release: move the entries under *Unreleased* into a new `## [X.Y.Z] - YYYY-MM-DD` section, bump `version` in
`pyproject.toml` to match, merge to `main`, let CD finish, then push the tag `vX.Y.Z`
([docs/delivery.md](docs/delivery.md), *Releasing*). The release workflow refuses a tag whose version or changelog
section does not match.

## [Unreleased]

### Added

- Signal sources and detection breadth (ADR-0020, implements D9 and the schema half of ADR-0009): collectors for notebook,
  Spark job definition and Dataflow runs; semantic model refresh outcomes from Fabric workspace monitoring (a parameterised,
  read-only KQL query; `ORQENTIS_REFRESH_SOURCE`); Delta schema changes read from the `_delta_log` of the contracts'
  new `delta_tables` through OneLake (rename only when column mapping proves it); a Capacity Metrics `executeQueries`
  adapter (dormant until configured; the spikes could not confirm its source offline). Deterministic rules for notebook,
  Spark job, Dataflow and refresh failures and for SLA breaches (severity from the data product's declared criticality or
  the environment), one incident per failure (a failure a failed pipeline activity already covers opens nothing), and an
  SLA breach is never declared while the freshness item's collector has no recent pass. Workflows for jobs, refreshes and
  SLA breaches on a shared `DiagnosticWorkflow` base; runbooks `RB-NOTEBOOK-FAILURE-001` and `RB-SLA-BREACH-001`;
  signatures for notebook exceptions and refresh errors; Spark's own `UNRESOLVED_COLUMN` message now yields the column.
  Known permission gaps are *capability unavailable* (metric `orqentis.collector.unavailable`, `unavailable` on
  `/health/ready`), not collector errors. Five new scenarios (`notebook_failure`, `refresh_failure`,
  `refresh_failure_pipeline`, `sla_breach`, `schema_drift_delta`); spike write-ups in `docs/spikes/`.
- Redaction no longer hides the error evidence's own `signature_match` (exact safe keys; the value is still scanned).
- Review round on the signal sources (PR 19): a hostile Delta commit (deeply nested JSON) no longer fails collection, and an
  unexpected error in one table's log is that table's error only; the schema in force is remembered per table
  (`SCHEMA_SNAPSHOT` events) so a change on a busy table is not missed, commits are read oldest first and the watermark
  never passes one that was not processed; a commit is read as a capped stream even if OneLake ignores `Range`, and a listing
  that hits its page limit is an error; workspace-monitoring scopes are the workspaces that hold a semantic model (those
  with no monitoring database are *capability unavailable*) and only rows naming a model of that workspace are used;
  only known launcher invoke types suppress a job incident; an ambiguous monitoring database and a Capacity Metrics schema
  mismatch are errors; an unavailable collector still records a run (`outcome=unavailable`); the refresh history tool
  follows `ORQENTIS_REFRESH_SOURCE`; Bicep parameters for the Delta log caps. From the first live checks: a Kusto 400
  for a missing `SemanticModelLogs` table is recognised from the error's `@message` (the generic `message` never named it);
  the monitoring database is recognised per workspace by its documented names instead of one configured name
  (`ORQENTIS_WORKSPACE_MONITORING_DATABASE` is now an override, empty by default), and the not-enabled error counts the KQL
  databases the reader could see.

- Durable storage and a stateless app (ADR-0017, implements ADR-0008): PostgreSQL adapters (asyncpg, Entra token as the
  password, no password setting) for the event store, incidents, dependencies, approvals (compare-and-set), the audit
  chain (chain-head row lock), the leader lease (database clock, fencing generation) and investigation jobs
  (`FOR UPDATE SKIP LOCKED`, heartbeats, re-claim after a crash); versioned SQL migrations behind an advisory lock;
  Blob evidence/report and audit-anchor adapters (write-once, version-level immutability, OneLake by URL).
  `ORQENTIS_STORAGE_BACKEND=postgres` is required in `dev`/`test`/`prod`; `local` stays the default for development,
  tests, mock mode and `make demo`.
- The estate contracts file is a blob in deployed environments (`contracts/estate.json` in the evidence account,
  read-only for the app, uploaded with `az storage blob upload --auth-mode login`): reloaded on every replica by the
  catalog refresh without a restart, SHA-256 and blob version audited on every (re)load (`contracts.loaded`), a bad
  upload keeps the last good contracts, and a missing or invalid blob at start-up leaves the app not ready (typed error,
  no crash loop). `ORQENTIS_CONTRACTS_PATH` stays for local and mock runs.
- Fenced writes: scheduled jobs run under the lease generation and every PostgreSQL write transaction checks it, so a
  deposed leader's late write is refused. Detection runs under a cross-replica mutex.
- Audit anchors: the chain head is written to an immutable blob every 15 minutes. Verification checks **every** anchor
  (the full hash is in the object name) and the operator-written epoch marker, reports the age of the newest anchor, and
  never raises a false alarm for an append that commits while it runs; the anchor job refuses to anchor a head that
  contradicts the previous anchor (ERROR `audit chain verification failed`). `orqentis audit start-epoch` (an operator
  action: its marker lives in the `audit-epochs` container that the application can only read) records a restore after
  verifying the chain, with an audited `--override-verification`. `orqentis db migrate|status|reseed-counters`.
- Safe against stalled or partitioned replicas: a worker that cannot renew its job claim stops, its database writes are
  fenced by the claim, there is at most one open approval (pending, or granted and not yet used) per action and one
  incident per detection (migration 0002, which cancels duplicates a database already holds instead of failing),
  the detection mutex cancels its section if its lock connection dies, approval expiry and audit timestamps use the
  database clock, neither the lease fence nor the claim fence starves a renewal or a heartbeat, a polling follower never
  queues behind the leader's writes, and a Fabric write is preceded by an `action.execute.intent` audit record (503
  `AUDIT_UNAVAILABLE`, nothing written, the incident back to `DIAGNOSED`, if it cannot be appended).
- Audit operators can set a junk anchor aside: `orqentis audit start-epoch --override-verification ... --disregard-anchor
  <object name>` records it in the new epoch's immutable marker and in the chain; an epoch marker whose commit never
  happened pins nothing, even if the app appended records before the retry. Documented in `docs/operations/restore.md`.
- `/health/ready` on the leader says `degraded: ["audit anchoring stale"]` when the anchor job has not succeeded for twice
  its interval; `scripts/postgres-bootstrap.sql` leaves the operators group with `SET TRUE` whether the group itself or one
  of its members runs it.
- Bicep: test and prod are private (VNet-integrated Container Apps environment and PostgreSQL, no "Allow Azure services"),
  `audit-epochs` container and operators group, an explicit `EVIDENCE_IMMUTABILITY_LOCKED` decision for prod, a connection
  budget that assumes overlapping revisions, and the Storage Blob Data Reader role id corrected.
- Bicep: PostgreSQL Flexible Server (B1ms default, Entra-only, PITR), evidence storage account (shared key disabled,
  versioning, version-level immutability), container-scoped role assignments, `minReplicas`/`maxReplicas`
  (1 in dev, 2 in test and prod). `docs/operations/restore.md`, `scripts/postgres-bootstrap.sql`.
- Contract tests for every storage port against the local and the PostgreSQL/Blob adapters, and concurrency tests with
  two instances, two OS processes, a killed process and a graceful stop; CI runs them with PostgreSQL and Azurite.
- Alerts can notify a separately managed action group (WS9b): `existingActionGroupId` / `alertExistingActionGroupId` (Bicep) and
  `ALERT_ACTION_GROUP_ID` (read by `env/*.bicepparam`, passed by the deploy job from a GitHub variable). The pipeline then carries only an
  identifier, so addresses never reach the job log of a public repository; the template creates and changes no action group and a
  malformed id or an id together with receivers stops the deployment with a readable reason. `scripts/check-alert-action-group.sh`
  evaluates both modes and the refusals offline in CI. Documented in `docs/operations.md` and `docs/delivery.md`.
- Observability and operations (ADR-0019): OpenTelemetry metrics behind `observability/metrics.py` (collection duration and
  errors per collector, events ingested, incidents opened and detection latency, investigation duration and outcome, tool errors,
  model tokens and estimated cost, pending approvals, scheduler job runs/failures/overruns, lease changes, and a collection
  heartbeat that moves only for a collection in which not everything failed); the scheduled `audit-verify` job (metric plus an
  ERROR log line on failure, optional immutable-anchor check); `infra/bicep/modules/monitoring.bicep` with an action group (email
  receivers and an optional Teams Workflows webhook, both deployment-time inputs), six alerts (app down, restart loop, 5xx rate,
  collector errors, collection heartbeat, audit verification failure) with parameterised windows and thresholds and runbook links,
  and a workbook; `docs/operations.md` and `docs/cost.md`. New settings `ORQENTIS_SCHEDULER_AUDIT_VERIFY_*` and
  `ORQENTIS_LLM_*_COST_PER_MILLION_TOKENS`; Bicep parameters `monitoringEnabled`, `alert*`, `auditVerify*`, `logDailyQuotaGb`,
  `llm*CostPerMillionTokens`. Nothing is exported unless Application Insights or the console exporter is configured.
- Notifications and a web UI (ADR-0018, implements ADR-0010). A `Notifier` port with a Microsoft Teams Workflows webhook adapter (an
  Adaptive Card with a link and no buttons), a generic JSON webhook adapter (optional HMAC-SHA256 signature) and a null adapter; incident
  opened / escalated, approval requested and action executed / failed are announced with deep links. Delivery is in the background:
  de-duplicated by event id, a bounded outbox, retries with back-off and jitter, one audit record per attempt, never blocking detection or
  an investigation. Content is sanitised, redacted and length-limited. Webhook URLs are Key Vault references; every call passes an SSRF
  guard (https, host allow list, public addresses after DNS, connection pinned to the checked address, no redirects).
- A server-rendered UI under `/ui` (Jinja2, no script): incident list with filters, incident detail, the report with FACT / INFERENCE /
  RECOMMENDATION labels, approvals with approve / reject / execute and a comment, audit verification. Same guards and services as the API;
  CSRF form tokens, strict Content-Security-Policy and security headers.
- `orqentis notify-test`, `docs/teams-notifications.md`, and Bicep parameters `notifierKind`, `notify*SecretUri`, `notifyAllowedHosts`,
  `publicBaseUrl`, `uiEnabled`, `uiSignInClientSecretUri` (interactive Easy Auth sign-in with a Key Vault client secret) and `uiCsrfSecretUri`.
- Delivery pipeline (ADR-0016): `cd.yml` runs CI first, then builds the image once, scans it with Trivy, generates an SPDX
  SBOM, pushes it to the shared registry by digest, tags it (`<git sha>` for `main`, `dryrun-<git sha>` otherwise) only
  after the scan gate passes, deploys `dev` and smoke-tests it, then promotes the same digest to `test` and `prod`
  through GitHub environments with required reviewers. Azure access is OIDC only, with separate build, per-environment
  and deploy identities whose federated-credential subjects use the repository's immutable-subject prefix.
- `bicep-diff.yml`: an offline diff of what each environment would deploy on pull requests (`bicep snapshot` of the base
  and the merged pull request, no Azure access, no credentials), and a recorded `az deployment group what-if` in each
  deploy job, run with the environment's own deploy identity before it deploys.
- `release.yml`: version-tag releases in a dedicated `release` environment, with notes from this file, the SBOM, the scan
  report and the image digest of the successful CD run of that commit on `main`.
- `infra/bicep/env/{dev,test,prod}.bicepparam` (production writes disabled) and `scripts/validate-bicep.sh`.
- `scripts/smoke-test.sh`: the latest revision is ready, serves all traffic and runs the deployed digest; `/health` is
  200; anonymous `GET /incidents` is 401; `GET /incidents` with the Automation app-role token is 200.
- `containerRegistryResourceId` Bicep parameter and `modules/acr-pull.bicep`: `AcrPull` for the reader identity on a
  registry in another resource group, such as the shared registry.
- `docs/delivery.md`: owner instructions for OIDC federated credentials, least-privilege role assignments (including the
  constrained RBAC Administrator and the nested-deployment role), GitHub environments, variables, and the rulesets that
  protect `main` and the release tags.
- `scripts/pip_audit_gate.py` and `.pip-audit-allowlist.toml`: dependency audit with justified, owned, expiring
  exceptions. `.trivyignore.yaml` for image scan exceptions. `scripts/release_tools.py` for release notes and evidence.

### Security

- Every state-changing request, API and UI, passes a cross-origin guard (`Origin`, `Sec-Fetch-Site`; `Referer` on `/ui`), and the API accepts
  JSON bodies only (415 otherwise). An ID-token browser session is a person, so without this a foreign page could
  `POST /approvals/{id}/approve` (ledger R6). Reads that spend a rate budget (`/ui/*`, `/audit/verify`) also refuse cross-site
  subresource loads, so a foreign page cannot use up a person's budget.
- Settings validation errors no longer print the raw environment (`hide_input_in_errors`), which showed the end of the webhook
  signature, signing key and CSRF key in a start-up traceback. Notification text is redacted before it is shortened; the HTTP
  client's loggers are pinned to WARNING; IPv6 site-local and `192.88.99.0/24` are refused as webhook destinations.

### Changed

- CI pins runners to `ubuntu-24.04` and every third-party action by commit SHA, lints workflows with actionlint, audits
  dependencies with pip-audit, scans the image with Trivy (any HIGH/CRITICAL fails), runs weekly, and is a reusable
  workflow that CD calls before it builds.
- The container image is based on `mcr.microsoft.com/azurelinux/base/python:3.12` (pinned by digest) instead of Debian
  `python:3.12-slim`: no HIGH or CRITICAL findings, no Docker Hub pull limit. `pip` is removed from the runtime image and
  the build fails if it is still present.
