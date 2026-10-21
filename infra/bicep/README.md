# Orqentis FabricOps: Azure deployment (Bicep)

`main.bicep` deploys FabricOps into **one resource group** on Azure Container Apps. The app is stateless: its system of record is an Azure Database for PostgreSQL Flexible Server, and its evidence is in Blob storage with version-level immutability ([ADR-0008](../../docs/adr/0008-production-storage.md); implementation choices in ADR-0017). The template contains no credentials. Identities are managed, PostgreSQL accepts Microsoft Entra tokens only (password authentication is disabled), storage Shared Key access is off, and the Application Insights connection string is stored as a Container App secret.

| Resource | Name | Purpose |
|---|---|---|
| Log Analytics workspace | `log-<prefix>` | Container Apps console/system logs; backing store for Application Insights |
| Application Insights (workspace-based) | `appi-<prefix>` | OpenTelemetry traces, logs and metrics from the app (`azure-monitor` extra) |
| User-assigned managed identity | `id-<prefix>-reader` | **READ** capability boundary. The app's operating identity (Fabric/Power BI read, Azure OpenAI) |
| User-assigned managed identity | `id-<prefix>-actor` | **WRITE** capability boundary. Created, but attached to the app **only** when `enableWriteActions = true` |
| PostgreSQL Flexible Server | `psql-<prefix>-<hash>` (`modules/postgres.bicep`) | The system of record: incidents, approvals, events, scheduler leases, investigation jobs, the audit trail. Microsoft Entra authentication only (`passwordAuth` disabled), an Entra administrator (`postgresEntraAdminObjectId`), database `orqentis`, point-in-time restore. See *Storage* |
| Storage account | `stev<prefix><hash>` (`modules/evidence-storage.bicep`) | Evidence, reports, audit anchors, audit epoch markers and the estate contracts file in Blob containers `evidence`, `audit-anchors`, `audit-epochs` and `contracts`. Entra only (Shared Key off), versioning, soft delete, version-level immutability |
| Role assignments | `modules/evidence-storage.bicep` | On the containers only, never on the account. **Reader** identity: `Storage Blob Data Contributor` on `evidence` and `audit-anchors`, `Storage Blob Data Reader` (read-only) on `audit-epochs` and `contracts`. Optional audit operators group (`auditOperatorsPrincipalId`): `Storage Blob Data Contributor` on `audit-epochs` only. The actor identity gets nothing on storage, and the template grants nobody the right to upload contracts (see *Storage*, who may write where) |
| Container Apps environment | `cae-<prefix>` | Workload-profiles environment (Consumption), logs to Log Analytics, optional VNet |
| Container App | `ca-<prefix>` | The API (`orqentis serve`), port 8000, `minReplicas` to `maxReplicas` replicas (2 by default; dev uses 1), Entra ID authentication, exposure per `ingressMode` |
| Container App auth config | `current` | Built-in authentication (Easy Auth) with Microsoft Entra ID, `Return401`; `/health` and `/health/ready` excluded by default (`anonymousHealthEndpoints`) |
| Role assignment (optional) | n/a | `AcrPull` for the reader identity on an ACR in the same RG (`containerRegistryName`) |
| Role assignment (optional) | `modules/acr-pull.bicep` | `AcrPull` for the reader identity on an ACR in any resource group (`containerRegistryResourceId`), such as the shared registry used by every environment |
| Role assignment (optional) | `modules/openai-role.bicep` | `Cognitive Services OpenAI User` for the reader identity on `azureOpenAiResourceId` (any RG/subscription) |
| Role assignment (optional) | `modules/keyvault-role.bicep` | `Key Vault Secrets User` for the reader identity on **each referenced secret only** (not the vault), in the vault of `keyVaultResourceId` (any RG/subscription) |
| Deployment check | `modules/input-check.bicep` | Fails the deployment with a readable reason when the `service_principal` inputs cannot work (see *Cross-tenant Fabric access*), and when the PostgreSQL administrator, the estate contracts (`rest` mode), the replica counts, the connection budget or the network options cannot work (see *Storage*) |
| Container App secrets (optional) | `fabric-sp-client-secret` / `fabric-sp-certificate` | Key Vault references (`keyVaultUrl` + the reader identity), only in `service_principal` mode |
| Container App secrets (optional) | `notify-teams-webhook-url`, `notify-webhook-url`, `notify-webhook-signing-secret`, `ui-csrf-secret`, `microsoft-provider-authentication-secret` | Key Vault references, only for the parameters that are set (see *Notifications and the web UI*) |
| Deployment check | `modules/input-check.bicep` (second instance) | Fails the deployment with a readable reason for malformed notification / sign-in inputs |
| Action group | `ag-<prefix>` | `modules/monitoring.bicep` (`monitoringEnabled`): email receivers and an optional Teams Workflows webhook, both supplied at deployment time. Not created when `alertExistingActionGroupId` names a group managed elsewhere |
| Alert rules (6) | `<prefix>-app-down`, `-restart-loop`, `-5xx-rate`, `-collector-errors`, `-collection-heartbeat`, `-audit-verification-failure` | One metric alert and five log alerts, each with severity, window, threshold parameter, the action group and a runbook link (see *Monitoring and alerts*) |
| Workbook | `Orqentis FabricOps (<prefix>)` | Collections and errors, detection latency, incidents, investigations, approvals, model tokens and cost, scheduler, restarts, 5xx |

The app runs with these settings (see `src/orqentis/config/settings.py`):

| Variable | Value |
|---|---|
| `ORQENTIS_DEPLOYMENT_ENVIRONMENT` | `deploymentEnvironment` (default `prod`) |
| `ORQENTIS_TENANT_ID` | `fabricTenantId` (empty, the default: the deployment tenant; **required** in `service_principal` mode) |
| `ORQENTIS_API_AUTH_MODE` | `easy_auth` (required outside `local`) |
| `ORQENTIS_AUTH_MODE` | `fabricAuthMode` (default `managed_identity`; `service_principal` for a Fabric tenant in another Entra tenant) |
| `ORQENTIS_MANAGED_IDENTITY_CLIENT_ID` | reader identity client id |
| `AZURE_CLIENT_ID` | reader identity client id, or `fabricServicePrincipalClientId` in `service_principal` mode |
| `AZURE_TENANT_ID`, `ORQENTIS_LLM_AUTH_MODE=managed_identity` | `service_principal` mode only: `fabricTenantId`; keeps the LLM credential on the reader identity |
| `AZURE_CLIENT_SECRET` **or** `AZURE_CLIENT_CERTIFICATE_PATH` | `service_principal` mode only: Container App secret reference to Key Vault (`fabricServicePrincipalSecretUri`), or the path of the PEM file mounted from `fabricServicePrincipalCertificateUri` |
| `ORQENTIS_DEFAULT_TIMEZONE` | `defaultTimezone` (default `Australia/Sydney`) |
| `ORQENTIS_AUTHORITY_HOST`, `ORQENTIS_FABRIC_API_BASE_URL`, `ORQENTIS_POWERBI_API_BASE_URL`, `ORQENTIS_FABRIC_SCOPE`, `ORQENTIS_POWERBI_SCOPE` | `authorityHost`, `fabricApiBaseUrl`, `powerbiApiBaseUrl`, `fabricScope`, `powerbiScope` (only set when non-empty; empty keeps the public cloud) |
| `ORQENTIS_FABRIC_MODE` | `fabricMode` (default `rest`) |
| `ORQENTIS_COLLECT_ITEM_JOBS`, `ORQENTIS_COLLECT_SCHEMA_CHANGES` | `collectItemJobs`, `collectSchemaChanges` (default `true`): notebook, Spark job and Dataflow runs; the Delta logs of the contracts' `delta_tables` |
| `ORQENTIS_DELTA_LOG_MAX_COMMITS`, `ORQENTIS_DELTA_LOG_MAX_COMMIT_BYTES` | `deltaLogMaxCommits` (default `50`, 1 to 1000), `deltaLogMaxCommitBytes` (default `2097152`, 1 KiB to 64 MiB): commits of one Delta table read per run (oldest unprocessed first; the rest on the next run), and bytes read of one commit file |
| `ORQENTIS_REFRESH_SOURCE`, `ORQENTIS_WORKSPACE_MONITORING_DATABASE` | `refreshSource` (default `workspace_monitoring`: the Power BI refresh history needs dataset Write), `workspaceMonitoringDatabase` (default empty: the monitoring database of each workspace is recognised by the documented naming rules; set a name only as an override) |
| `ORQENTIS_CAPACITY_METRICS_WORKSPACE_ID` / `ORQENTIS_CAPACITY_METRICS_DATASET_ID` | `capacityMetricsWorkspaceId` / `capacityMetricsDatasetId` (both or neither; the deployment fails with a readable reason otherwise) |
| `ORQENTIS_EXPECTED_FORBIDDEN_COLLECTORS` | `expectedForbiddenCollectors` (comma-separated collector names whose HTTP 403 is documented as expected: reported as capability unavailable, not as an error) |
| `ORQENTIS_SLA_MAX_TELEMETRY_LAG_MINUTES` | `slaMaxTelemetryLagMinutes` (default `30`; `0` turns the telemetry check off) |
| `ORQENTIS_KUSTO_SCOPE`, `ORQENTIS_ONELAKE_BASE_URL`, `ORQENTIS_ONELAKE_SCOPE` | `kustoScope`, `onelakeBaseUrl`, `onelakeScope` (only set when non-empty; empty keeps the public cloud) |
| `ORQENTIS_CONTRACTS_CONTAINER` / `ORQENTIS_CONTRACTS_BLOB_NAME` | `contractsContainerName` / `contractsBlobName` (default `contracts` / `estate.json`): where the estate contracts file is read from, in the evidence account (`ORQENTIS_EVIDENCE_BLOB_URL`). No path and no volume; see *Estate contracts* |
| `ORQENTIS_ENABLE_WRITE_ACTIONS` | `enableWriteActions` (default `false`) |
| `ORQENTIS_REASONER` | `reasoner` (default `rule_based`) |
| `ORQENTIS_AZURE_OPENAI_ENDPOINT` / `_DEPLOYMENT` | `azureOpenAiEndpoint` / `azureOpenAiDeployment` (only set when non-empty) |
| `ORQENTIS_LLM_INPUT_COST_PER_MILLION_TOKENS` / `ORQENTIS_LLM_OUTPUT_COST_PER_MILLION_TOKENS` | `llmInputCostPerMillionTokens` / `llmOutputCostPerMillionTokens` (USD, decimal text; only set when non-empty; they only feed the estimated-cost metric) |
| `ORQENTIS_DATA_DIR` | `/var/lib/orqentis`: the container's own writable disk, ephemeral and per replica (no volume; nothing durable is kept there) |
| `ORQENTIS_LOG_FORMAT` | `json` |
| `ORQENTIS_SCHEDULER_ENABLED` | `schedulerEnabled` (default `true`) |
| `ORQENTIS_SCHEDULER_STARTUP_DELAY_SECONDS` | `schedulerStartupDelaySeconds` (default `30`; was `120`). A courtesy only: the database lease is what guarantees a single scheduler |
| `ORQENTIS_SCHEDULER_TICK_SECONDS` / `_LEASE_TTL_SECONDS` / `_JOB_TIMEOUT_MINUTES` | `schedulerTickSeconds` / `schedulerLeaseTtlSeconds` / `schedulerJobTimeoutMinutes` (default `5` / `60` / `30`; the TTL must be at least three times the tick) |
| `ORQENTIS_SCHEDULER_COLLECT_ENABLED` / `_INTERVAL_MINUTES` | `collectEnabled` / `collectIntervalMinutes` (default `true` / `5`) |
| `ORQENTIS_SCHEDULER_AUTO_INVESTIGATE_ENABLED` / `_INTERVAL_MINUTES` | `autoInvestigateEnabled` / `autoInvestigateIntervalMinutes` (default `true` / `1`) |
| `ORQENTIS_SCHEDULER_AUTO_INVESTIGATE_BATCH_SIZE` / `_MAX_ATTEMPTS` | `autoInvestigateBatchSize` / `autoInvestigateMaxAttempts` (default `5` / `3`) |
| `ORQENTIS_SCHEDULER_SLA_SWEEP_ENABLED` / `_INTERVAL_MINUTES` | `slaSweepEnabled` / `slaSweepIntervalMinutes` (default `true` / `15`) |
| `ORQENTIS_SCHEDULER_CATALOG_REFRESH_ENABLED` / `_INTERVAL_MINUTES` | `catalogRefreshEnabled` / `catalogRefreshIntervalMinutes` (default `true` / `60`) |
| `ORQENTIS_SCHEDULER_AUDIT_VERIFY_ENABLED` / `_INTERVAL_MINUTES` | `auditVerifyEnabled` / `auditVerifyIntervalMinutes` (default `true` / `60`): the audit chain is verified on a schedule; failure raises the audit-verification alert |
| `ORQENTIS_INVESTIGATION_MAX_CONCURRENCY` / `_TIMEOUT_SECONDS` | `investigationMaxConcurrency` / `investigationTimeoutSeconds` (default `2` / `600`) |
| `ORQENTIS_INVESTIGATION_POLL_INTERVAL_SECONDS` / `ORQENTIS_INVESTIGATION_CLAIM_TTL_SECONDS` | `investigationPollIntervalSeconds` / `investigationClaimTtlSeconds` (default `2` / `120`; the TTL must be at least three times the poll interval). Every replica polls for queued durable investigation jobs; a claim that is not renewed within the TTL is released to another replica |
| `ORQENTIS_SCHEDULER_AUDIT_ANCHOR_ENABLED` / `ORQENTIS_SCHEDULER_AUDIT_ANCHOR_INTERVAL_MINUTES` | `auditAnchorEnabled` / `auditAnchorIntervalMinutes` (default `true` / `15`): the audit chain head is written to an immutable blob on this schedule |
| `ORQENTIS_STORAGE_BACKEND` | `postgres`, always. `local` (SQLite and files) is for development and tests only, and the app refuses it outside `local` |
| `ORQENTIS_POSTGRES_HOST` / `ORQENTIS_POSTGRES_DATABASE` | the server's FQDN (output `postgresServerFqdn`) / `postgresDatabaseName` (default `orqentis`) |
| `ORQENTIS_POSTGRES_USER` | the reader identity's **name**, `id-<prefix>-reader`: the PostgreSQL role the bootstrap step creates for the managed identity. The identity's Entra token is the password; there is no other credential |
| `ORQENTIS_POSTGRES_POOL_MAX` | `postgresPoolMax` (default `6`, at least `2`): connections per replica; see the connection budget under *Storage* |
| `ORQENTIS_EVIDENCE_BLOB_URL` / `ORQENTIS_EVIDENCE_CONTAINER` / `ORQENTIS_AUDIT_ANCHOR_CONTAINER` | the evidence account's blob endpoint (output `evidenceBlobEndpoint`) / `evidenceContainerName` (default `evidence`) / `auditAnchorContainerName` (default `audit-anchors`) |
| `ORQENTIS_AUDIT_EPOCH_CONTAINER` | `auditEpochContainerName` (default `audit-epochs`): the epoch-start markers an operator writes after a restore. The app can only **read** it |
| `ORQENTIS_FABRIC_MAX_CONCURRENCY` / `_RATE_PER_SECOND` / `_RATE_BURST` | `fabricMaxConcurrency` / `fabricRatePerSecond` / `fabricRateBurst` (default `4` / `5` / `10`) |
| `ORQENTIS_FABRIC_JOB_INSTANCES_MAX_PAGES` | `fabricJobInstancesMaxPages` (default `10`) |
| `ORQENTIS_STARTUP_GRACE_SECONDS` / `ORQENTIS_BUILD_RETRY_INITIAL_SECONDS` / `_MAX_SECONDS` | `startupGraceSeconds` / `buildRetryInitialSeconds` / `buildRetryMaxSeconds` (default `10` / `2` / `60`) |
| `ORQENTIS_COLLECTION_MAX_LOOKBACK_HOURS` / `_OVERLAP_MINUTES` | `collectionMaxLookbackHours` / `collectionOverlapMinutes` (default `168` / `15`) |
| `ORQENTIS_API_MAX_BODY_BYTES` | `apiMaxBodyBytes` (default `65536`): larger request bodies get 413 |
| `ORQENTIS_API_RATE_LIMIT_MUTATING_PER_MINUTE` | `apiRateLimitMutatingPerMinute` (default `30`), per principal and replica |
| `ORQENTIS_API_RATE_LIMIT_EXPENSIVE_PER_MINUTE` | `apiRateLimitExpensivePerMinute` (default `6`): collection, investigations, audit verification |
| `ORQENTIS_NOTIFIER` / `ORQENTIS_PUBLIC_BASE_URL` / `ORQENTIS_UI_ENABLED` | `notifierKind` (default `null`) / `publicBaseUrl` (default: derived from the environment) / `uiEnabled` (default `true`) |
| `ORQENTIS_NOTIFY_ALLOWED_HOSTS` | `notifyAllowedHosts` (only set when non-empty) |
| `ORQENTIS_NOTIFY_TEAMS_WEBHOOK_URL`, `ORQENTIS_NOTIFY_WEBHOOK_URL`, `ORQENTIS_NOTIFY_WEBHOOK_SIGNING_SECRET`, `ORQENTIS_UI_CSRF_SECRET` | Container App **secret** references to Key Vault, each set only when its `...SecretUri` parameter is |
| `ORQENTIS_UI_SIGN_IN_PATH` | `/.auth/login/aad`, only when `uiSignInClientSecretUri` is set |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | Container App **secret** reference |

Not set by the template, because the safe default already applies under `easy_auth`: `ORQENTIS_API_REQUIRE_READ_ROLE` (every call needs a FabricOps role; see section 1) and `ORQENTIS_API_RATE_LIMIT_ENABLED` (`true`). Also left at their defaults: `ORQENTIS_POSTGRES_SCHEMA` (`orqentis`), `ORQENTIS_POSTGRES_AUTH` (`entra`), `ORQENTIS_POSTGRES_SSL_MODE` (`verify-full`), `ORQENTIS_POSTGRES_MIGRATE_ON_START` (`true`: the app applies pending schema migrations itself when it starts, behind a database advisory lock) and `ORQENTIS_EVIDENCE_PATH_PREFIX` / `ORQENTIS_AUDIT_ANCHOR_PATH_PREFIX` (empty).

### Health probes and autonomous operation

The Container App has three probes (see the `probes` block of `main.bicep`):

| Probe | Path | Meaning |
|---|---|---|
| Startup | `/health` | The process is listening. It does not wait for the estate to be read: that happens in the background after the port opens, so a large tenant cannot fail the startup probe. |
| Liveness | `/health` | The process is alive. It is dependency-free and answers `200` while the container is still being built and while a failed build is being retried, so a bad contracts file or a Fabric outage never causes a restart loop. |
| Readiness | `/health/ready` | `503` (a fixed reason code and the exception type, never its message) until the container is built and the first catalog load finished, `200` afterwards. A failing build is retried with exponential backoff (`ORQENTIS_BUILD_RETRY_*`). |

With `schedulerEnabled = true` (the default) the app runs a small in-process scheduler: collect + detect every `collectIntervalMinutes`, automatic investigation of new incidents (as background jobs, at most `investigationMaxConcurrency` at a time), an SLA sweep, and a catalog refresh that applies workspace / item / dependency changes without a restart. Only the holder of the leader lease runs the jobs. The lease is a row in the PostgreSQL database: its expiry is measured on the database clock and every acquisition increases a fencing generation that renewals and fenced writes check, so exactly one replica schedules work however many run, and a replica that lost the lease cannot complete a write made under it. Investigations are durable jobs in the same database: `POST /incidents/{id}/investigate` can land on any replica, any replica claims queued jobs (every `investigationPollIntervalSeconds`), and a claim that its replica stops renewing for `investigationClaimTtlSeconds` is released to another replica. The audit chain head is anchored to an immutable blob every `auditAnchorIntervalMinutes`. All Fabric and Power BI calls share one concurrency and rate budget (`fabricMaxConcurrency`, `fabricRatePerSecond`, `fabricRateBurst`), and a `429` / `Retry-After` pauses every caller. Defaults are deliberately conservative; the real Fabric throttling quotas are not published per endpoint, so tune them from the `fabric http request` log lines (`status_code: 429`).

### Deploying an update: two revisions run side by side

Container Apps in `Single` revision mode runs the **old and the new revision side by side** while an update rolls out, and each runs two or more replicas (`minReplicas` / `maxReplicas`, 2 by default in `test` and `prod`, 1 in `dev`). That is safe because no replica keeps state of its own: the database makes every decision that more than one process could make.

* **One audit chain.** An append locks the single chain-head row (`SELECT ... FOR UPDATE`), reads the last sequence and hash, inserts the record and moves the head in one transaction, so appends from any number of replicas, old revision and new revision included, form one linear chain. `GET /audit/verify` / `orqentis audit` verify it, and check it against every anchor and epoch marker.
* **One scheduler.** The leader lease is a database row with a fencing generation. The revision being replaced releases it when it receives the shutdown signal, the new one takes it over, and a replica that was paused and wakes up after losing the lease cannot write under it.
* **Approvals execute at most once.** A state change is stored only when the approval's revision is still the one the caller read (`UPDATE ... WHERE revision = $n`), so of N concurrent executes across replicas exactly one wins and exactly one write happens.
* **Jobs survive a replica.** Investigation jobs are rows: one active job per incident, claimed by any replica. A job that the stopping revision was running is claimed again after `investigationClaimTtlSeconds`, so a deploy loses no job.
* **Schema changes are expand-only.** Migrations are forward-only and each is backward compatible with the previous release (expand now, contract in a later release), and the app applies them at start behind an advisory lock, so the old revision keeps working while the new one migrates.

`schedulerStartupDelaySeconds` (default `30`; it was `120` while the stores were single-writer files) is now only a courtesy: a starting revision does not compete for the lease while it warms up. The safety comes from the lease, not from this delay. The old and the new revision hold database connections at the same time, which is what the connection budget under *Storage* accounts for. If the new revision never becomes ready (`/health/ready`), the old one keeps serving. Verify the chain after a deployment if you want to see it: `orqentis audit` or `GET /audit/verify`.

Settings that fail validation (for example a missing `ORQENTIS_TENANT_ID` in rest mode, or a lease TTL shorter than three scheduler ticks) stop the app at start by design: that is deploy-time misconfiguration, and the first failed startup probe is the signal, not something to retry.

## Deploy

**Pipeline.** `dev`, `test` and `prod` are deployed by GitHub Actions (`.github/workflows/cd.yml`) from `env/dev.bicepparam`, `env/test.bicepparam` and `env/prod.bicepparam`: one image is built and scanned once, then promoted by digest, with approval before `test` and `prod`. The one-time setup (OIDC identities, role assignments, GitHub environments and variables) is in [docs/delivery.md](../../docs/delivery.md). The steps below are the manual path: a first deployment, a new customer, or a repair.

Prerequisites: Azure CLI with Bicep (`az bicep install`), permission to create resources and role assignments in the target resource group (Owner, or Contributor + Role Based Access Control Administrator), and the ability to create an Entra app registration.

### 1. Register the API in Microsoft Entra ID

Container Apps authentication validates bearer tokens issued for this app registration. Unless you turn on browser sign-in for the web UI (*Interactive sign-in for the UI*, which adds a redirect URI and a client secret held in Key Vault), it never runs an interactive login, so no client secret is needed. This registration is documentation for a person with the right Entra permissions to run once. The repository's automation never runs it.

```bash
API_APP_ID=$(az ad app create --display-name "Orqentis FabricOps API" \
  --sign-in-audience AzureADMyOrg --query appId -o tsv)
az ad app update --id "$API_APP_ID" --identifier-uris "api://$API_APP_ID"
# v2 access tokens: their issuer matches the v2.0 openIdIssuer the template configures.
az ad app update --id "$API_APP_ID" --set api.requestedAccessTokenVersion=2

# Optional claim idtyp on access tokens. Entra then sets idtyp=app on app-only tokens (service principals and
# managed identities), the most reliable signal the API has that a caller is an application and not a person.
# The API also recognises app tokens by sub == oid, but idtyp is the signal Microsoft documents as the most
# accurate, and a second independent signal keeps the classification robust, so keep this claim enabled.
cat > optional-claims.json <<EOF
{ "accessToken": [ { "name": "idtyp", "essential": false } ] }
EOF
az ad app update --id "$API_APP_ID" --optional-claims @optional-claims.json

# App roles. Operator and PlatformAdmin are approver roles for people; Automation is for applications only.
cat > app-roles.json <<EOF
[
  {"allowedMemberTypes": ["User"], "isEnabled": true, "id": "$(uuidgen)",
   "value": "FabricOps.Operator", "displayName": "FabricOps Operator",
   "description": "Investigate incidents and approve production and non-production write actions."},
  {"allowedMemberTypes": ["User"], "isEnabled": true, "id": "$(uuidgen)",
   "value": "FabricOps.PlatformAdmin", "displayName": "FabricOps Platform Admin",
   "description": "Everything an operator can do, plus approve HIGH_RISK actions (capacity, deployments, permissions)."},
  {"allowedMemberTypes": ["Application"], "isEnabled": true, "id": "$(uuidgen)",
   "value": "FabricOps.Automation", "displayName": "FabricOps Automation",
   "description": "For schedulers, smoke tests and other applications: read endpoints and POST /telemetry/collect. Can never approve, reject or execute."},
  {"allowedMemberTypes": ["User", "Application"], "isEnabled": true, "id": "$(uuidgen)",
   "value": "FabricOps.Reader", "displayName": "FabricOps Reader",
   "description": "Read-only access to incidents, evidence, runbooks, estate, approvals and audit. Cannot trigger anything."}
]
EOF
az ad app update --id "$API_APP_ID" --app-roles @app-roles.json

# Only users, groups and applications assigned to the app can get a token for it.
az ad sp create --id "$API_APP_ID"
az ad sp update --id "$API_APP_ID" --set appRoleAssignmentRequired=true
```

`--app-roles` replaces the whole list, and every role keeps its `id`. To add a role to a registration that people are already assigned to, append to what is there instead of regenerating the file:

```bash
az ad app show --id "$API_APP_ID" --query appRoles -o json > current-roles.json
jq --arg id "$(uuidgen)" '. + [{"allowedMemberTypes": ["Application"], "isEnabled": true, "id": $id,
  "value": "FabricOps.Automation", "displayName": "FabricOps Automation",
  "description": "For schedulers, smoke tests and other applications: read endpoints and POST /telemetry/collect."}]' \
  current-roles.json > app-roles.json
az ad app update --id "$API_APP_ID" --app-roles @app-roles.json
```

Then, in the portal under **App registrations > Orqentis FabricOps API > Expose an API**:

* Add a delegated scope, e.g. `FabricOps.Access`.
* Add the client applications people use to call the API as authorized clients. For example, add Azure CLI (`04b07795-8ddb-461a-bbee-02f9e1bf7b46`) for operator testing with `az account get-access-token --scope api://<API_APP_ID>/FabricOps.Access`.

**What each role allows** (enforced in `orqentis.api.authz`; `ORQENTIS_API_REQUIRE_READ_ROLE` is on by default under Easy Auth):

| Role | Assigned to | Read endpoints | `POST /telemetry/collect` | Investigate | Approve / reject / execute |
|---|---|---|---|---|---|
| `FabricOps.Reader` | people, applications | yes | no | no | no |
| `FabricOps.Automation` | applications only | yes | yes | no | **never** |
| `FabricOps.Operator` | people | yes | yes | yes | LOW/MEDIUM risk |
| `FabricOps.PlatformAdmin` | people | yes | yes | yes | HIGH_RISK (an approval's required role is matched exactly, see `default_policy.yaml`) |

A caller with no FabricOps role gets 403 on every endpoint except `/health`. Collection and investigation always need their roles under Easy Auth (`ORQENTIS_API_REQUIRE_READ_ROLE=false` opens plain reads only). The interactive docs (`/docs`, `/redoc`) and `/openapi.json` are not served under Easy Auth, because they would answer any authenticated caller, whatever its roles. Approving and executing additionally require a **person**: the API classifies each caller from the token claims, and an app-only token (service principal, managed identity) is a service and is refused whatever roles it carries. Keep `allowedMemberTypes` as above anyway, so Entra itself never assigns an approver role to an application. See [ADR-0015](../../docs/adr/0015-principal-classification-and-api-hardening.md) for the decision table.

**Assign the app roles to approvers.** Use **Enterprise applications > Orqentis FabricOps API > Users and groups > Add user/group**, choose `FabricOps.Operator` or `FabricOps.PlatformAdmin` (or `FabricOps.Reader` for read-only people), and select the people or groups. Assigning groups needs Entra ID P1/P2. The assigned roles appear in the token's `roles` claim. Container Apps authentication forwards that claim in `X-MS-CLIENT-PRINCIPAL`, and `orqentis.api.auth` turns it into the caller's roles.

**Give an automation identity (scheduler, smoke test) the Automation role.** The identity is a managed identity or an app registration's service principal, never a person. Assign the app role with Microsoft Graph (`AppRoleAssignment.ReadWrite.All`, or Cloud Application Administrator, is needed):

```bash
API_SP_ID=$(az ad sp show --id "$API_APP_ID" --query id -o tsv)
AUTOMATION_ROLE_ID=$(az ad sp show --id "$API_APP_ID" \
  --query "appRoles[?value=='FabricOps.Automation'].id | [0]" -o tsv)
# Object id of the caller: a managed identity's principalId (az identity show ... --query principalId)
# or a service principal's object id (az ad sp show --id <its appId> --query id).
CALLER_PRINCIPAL_ID=<object id of the managed identity or service principal>
az rest --method post \
  --url "https://graph.microsoft.com/v1.0/servicePrincipals/$API_SP_ID/appRoleAssignedTo" \
  --headers "Content-Type=application/json" \
  --body "{\"principalId\": \"$CALLER_PRINCIPAL_ID\", \"resourceId\": \"$API_SP_ID\", \"appRoleId\": \"$AUTOMATION_ROLE_ID\"}"
```

The caller then requests a token for the scope `api://<API_APP_ID>/.default` with its own credential (a managed identity, or a federated workload identity; no client secret is needed or wanted) and sends it as `Authorization: Bearer`. Do not give the same identity `FabricOps.Reader` as well: Automation already includes read access.

**Check what the API will see.** Decode the access token (`jwt.ms` shows the claims locally; do not paste production tokens into third-party sites) and look for `"idtyp": "app"`, `sub` equal to `oid`, a `roles` claim with `FabricOps.Automation` and no `scp`. For people expect `scp` (delegated tokens) and a `sub` that differs from `oid`. To see how the API classifies a forwarded principal, paste a captured `X-MS-CLIENT-PRINCIPAL` value into:

```bash
python - <<'PY'
from orqentis.api.claims import classify, parse_client_principal
client = parse_client_principal(input("X-MS-CLIENT-PRINCIPAL: "))
decision = classify(client)
print(decision.kind, "ambiguous" if decision.ambiguous else "clear", decision.rule, "|", decision.detail)
print("roles:", sorted(client.roles))
PY
```

A principal that is neither clearly a person nor clearly an application is refused with `403 PRINCIPAL_AMBIGUOUS`, and the app logs the rule that decided.

### 2. Build and push the image

```bash
RG=rg-fabricops-prod
az group create -n "$RG" -l australiaeast
az acr create -g "$RG" -n crfabricops --sku Basic          # admin user stays disabled
az acr build -r crfabricops -t orqentis-fabricops:0.1.0 .   # builds the repository Dockerfile in Azure
```

### 3. Deploy the template

Copy `main.bicepparam` and set `entraClientId` to `$API_APP_ID`, along with `containerImage`, `containerRegistryName` and the other values you need. The PostgreSQL Entra administrator is **required** (`postgresEntraAdminObjectId` and `postgresEntraAdminName`: the object id and name of an Entra group whose members will run the bootstrap step below; the deployment fails with a readable reason when they are empty). In `rest` mode the estate contracts file is uploaded after the deployment (step 4). The subscription needs the `Microsoft.DBforPostgreSQL` resource provider registered once (`az provider register --namespace Microsoft.DBforPostgreSQL`).

```bash
az deployment group what-if -g "$RG" -f infra/bicep/main.bicep -p infra/bicep/main.bicepparam
az deployment group create  -g "$RG" -f infra/bicep/main.bicep -p infra/bicep/main.bicepparam -n fabricops
az deployment group show -g "$RG" -n fabricops --query properties.outputs
```

When `containerRegistryName` names an ACR in the same resource group, the template grants the reader identity `AcrPull` and pulls with it. For a registry in another resource group (the shared registry the pipeline uses), set `containerRegistryServer` to its login server and `containerRegistryResourceId` to its resource id: the template then grants the reader identity `AcrPull` on it through `modules/acr-pull.bicep` before it creates the app, so the deploying principal needs a role assignment right on that registry and the right to create deployments in the registry's resource group, because the module is a nested deployment there (docs/delivery.md, section 5). If you set only `containerRegistryServer`, grant `AcrPull` to the `readerIdentityPrincipalId` output yourself; the first revision fails to pull until that grant exists. After granting, restart the revision.

### 3a. PostgreSQL bootstrap (once per environment)

The template creates the server and its Entra administrator, but a template cannot create database roles. Until the app's managed identity exists **inside** PostgreSQL the app cannot connect, and `/health/ready` answers `503` (`/health` stays `200`). A member of the Entra administrator group (`postgresEntraAdminName`) creates the role once per environment:

* `az login` as that person, `psql` 16 or later (for `sslrootcert=system`; see the TLS note below), and network access to the server. In `test` and `prod` the server is private (*Private networking*): run this from a host inside the network, or one that reaches it through a peered network or VPN. In `dev` (public access) the server's firewall must allow your address: add it to `postgresAllowedIpRanges` and redeploy, or create a temporary rule (`az postgres flexible-server firewall-rule create -g "$RG" -s "$SERVER" -n bootstrap-me --start-ip-address <your address> --end-ip-address <your address>`) and delete it afterwards (`firewall-rule delete`).
* No password exists to type: the Entra access token is the password.

```bash
RG=rg-fabricops-dev                  # the environment's resource group
DEPLOYMENT=<deployment name>         # az deployment group list -g "$RG" --query "[].name" -o tsv   (the pipeline names it fabricops-<run id>-<attempt>)
out() { az deployment group show -g "$RG" -n "$DEPLOYMENT" --query "properties.outputs.$1.value" -o tsv; }
SERVER=$(out postgresServerName)
FQDN=$(out postgresServerFqdn)
DB=$(out postgresDatabaseName)                                      # orqentis
READER_OID=$(out readerIdentityPrincipalId)                         # the managed identity's object id
READER_ROLE=$(out readerIdentityResourceId | awk -F/ '{print $NF}')  # id-<prefix>-reader: the database role to create
ADMIN="<postgresEntraAdminName>"     # the admin group's display name (or the user's UPN); members use the group name as the user

# (a) The template is deployed (step 3). (b) Sign in with your own Entra token and run the bootstrap script from the repository root.
export PGPASSWORD="$(az account get-access-token --resource-type oss-rdbms --query accessToken -o tsv)"
psql "host=$FQDN port=5432 dbname=postgres user=$ADMIN sslmode=verify-full sslrootcert=system" -v ON_ERROR_STOP=1 \
  -f scripts/postgres-bootstrap.sql \
  -v app_role="$READER_ROLE" -v app_oid="$READER_OID" -v app_db="$DB" -v app_schema=orqentis
unset PGPASSWORD
```

`scripts/postgres-bootstrap.sql` does this, and only this, and is safe to run again:

1. creates the Entra principal for the managed identity with `pgaadauth_create_principal_with_oid` (so that identity's token maps to the role `id-<prefix>-reader`). If a role of that name already exists but is **not** that Entra principal (it compares the role name and the object id against `pgaadauth_list_principals(false)`), it stops with an error instead of treating the name as enough;
2. creates the schema `orqentis`, owned by that role;
3. removes what `PUBLIC` would otherwise hold (`CONNECT` and `TEMPORARY` on the database, `USAGE` on the `public` schema), then lets the application role, the administrators (`azure_pg_admin`) and the person running it connect, and nothing more. A `REVOKE` the person lacks the right to make becomes a warning, and the `privileges` column printed at the end shows what `PUBLIC` still holds;
4. optionally (below), lets an operators group act as the application role.

It contains no secret. The token lasts about an hour; if `psql` reports an expired token, run `az account get-access-token` again.

**TLS.** The commands use `sslmode=verify-full`, which checks the server certificate and its host name, and `sslrootcert=system`, which takes the roots from the operating system's trust store (libpq 16 or later). Azure Database for PostgreSQL certificates chain to DigiCert Global Root G2 or Microsoft RSA Root CA 2017 (Microsoft's SSL/TLS page for the service lists the current roots and any rotation; check it, I could not verify it offline); most trust stores hold both. With an older `psql`, or a trust store that lacks them, save the root certificates as PEM in one file and use `sslrootcert=<that file>`. The app uses `verify-full` too (`ORQENTIS_POSTGRES_SSL_ROOT_CERT` names an extra bundle if the image's store is missing a root).

**Optional: operators who open an audit epoch after a restore.** The Entra administrator group has **no rights on the application's tables** (the application role owns them), so it cannot run `orqentis audit start-epoch` ([docs/operations/restore.md](../../docs/operations/restore.md)) as itself. Pass `-v operator_role="<the group's database role>"` (the administrator group's own name, or another Entra group created with `pgaadauth_create_principal_with_oid`) and the script runs `GRANT "<app role>" TO "<operator role>" WITH INHERIT FALSE, SET TRUE` and lets the group connect: its members cannot touch the tables by accident, but can deliberately switch to the application role. They then sign in (as the group, which is how Entra administrators normally connect, or as themselves when they are members of it) with `ORQENTIS_POSTGRES_SET_ROLE=<app role>`. To create the schema the person running the script needs the right to `SET ROLE` to the application role, which PostgreSQL 16 and later do not give the creator of a role; the script grants it to them for the duration of the run and takes it back before the operators grant, so the outcome is the same whether the group itself runs the script or one of its members does: the group holds `SET TRUE, INHERIT FALSE`, and an individual who ran it keeps no right of their own beyond the group's. It needs PostgreSQL 16 or later (the template's default is 17) and is skipped when `operator_role` is not given. The same people also need write access to the `audit-epochs` container (*Storage*, who may write where).

**(c) Schema.** The app applies pending migrations itself when it starts (`ORQENTIS_POSTGRES_MIGRATE_ON_START`, default `true`), as its own database role and behind a database advisory lock, so several replicas starting together are safe. Because the schema belongs to the app's role, a person cannot run `orqentis db migrate` (it refuses any role but the schema owner: tables an administrator created would be invisible to the app). To look at the state, with your own token (any role that can read the schema):

```bash
export ORQENTIS_STORAGE_BACKEND=postgres ORQENTIS_POSTGRES_HOST="$FQDN" ORQENTIS_POSTGRES_DATABASE="$DB"   ORQENTIS_POSTGRES_USER="$ADMIN" ORQENTIS_EVIDENCE_BLOB_URL="$(out evidenceBlobEndpoint)"
orqentis db status      # applied and pending migrations
```

Set `ORQENTIS_POSTGRES_MIGRATE_ON_START=false` only if a release step runs `orqentis db migrate` **as the reader identity** (for example a one-off Container Apps job that uses the app image and the reader identity) before the rollout; the app then refuses to start on an older schema.

**(d) Verify.** Restart the revision so it connects without waiting for its retry, then check readiness:

```bash
APP=$(out containerAppName)
az containerapp revision restart -g "$RG" -n "$APP" \
  --revision "$(az containerapp revision list -g "$RG" -n "$APP" --query "[?properties.active].name | [0]" -o tsv)"
curl -fsS "https://$(out containerAppFqdn)/health/ready"       # 200 once the app is connected and the catalog is loaded
```

If `/health/ready` stays `503`, read the replica log (`az containerapp logs show -g "$RG" -n "$APP" --follow`): `role "id-..." does not exist` means the bootstrap did not run against this server and database; a connection timeout means the firewall or the network (in `dev`, public access needs `postgresAllowAzureServices` or a rule for the environment's outbound addresses; in `test` and `prod`, the private network: the Container Apps subnet must reach the PostgreSQL subnet, and the private DNS zone must be linked to the VNet); an Entra token error points at `ORQENTIS_MANAGED_IDENTITY_CLIENT_ID` or at the role being created for another identity.


### 4. Estate contracts (rest mode)

In `rest` mode the app needs the declared environments, topology, data products and SLAs: the **estate contracts file**. There is no shared disk and no secret for it. It is a **blob** in the evidence storage account (container `contracts`, blob `estate.json` by default: the parameters `contractsContainerName` and `contractsBlobName`, the settings `ORQENTIS_CONTRACTS_CONTAINER` and `ORQENTIS_CONTRACTS_BLOB_NAME`), and the account is the one in `ORQENTIS_EVIDENCE_BLOB_URL`. The template creates the container (versioned, soft delete, version-level immutability, like the other two) and gives the reader identity read-only access to it: **Storage Blob Data Reader**, scoped to that container, so a compromised app identity cannot rewrite the declared estate. You upload the file after the deployment. `mock` mode does not read it.

The file format is described by `schemas/contracts.schema.json`; a full estate file (`fixtures/fabric/estate.json`) is also accepted, and only its contract sections are used. Check it **before** uploading:

```bash
orqentis contracts scaffold --output contracts/estate.json --catalog-output contracts/catalog.json   # draft from a read-only listing
# ... review and edit the draft (see docs/development.md, "Contracts workflow") ...
orqentis contracts validate contracts/estate.json --catalog contracts/catalog.json --strict
```

**Who may upload.** The template assigns **no one** the right to write to the contracts container, and the pipeline's deploy identities cannot. The owner grants `Storage Blob Data Contributor` on **that container only** to each person, group or identity that uploads (this needs Owner or User Access Administrator; the deploy identity cannot):

```bash
RG=rg-fabricops-dev                  # the environment's resource group
DEPLOYMENT=<deployment name>         # az deployment group list -g "$RG" --query "[].name" -o tsv
out() { az deployment group show -g "$RG" -n "$DEPLOYMENT" --query "properties.outputs.$1.value" -o tsv; }
ACCOUNT=$(out evidenceStorageAccountName)
CONTAINER=$(out contractsContainerName)                                                    # contracts
SCOPE="$(az storage account show -g "$RG" -n "$ACCOUNT" --query id -o tsv)/blobServices/default/containers/$CONTAINER"

# In Git Bash on Windows prefix the next command with MSYS_NO_PATHCONV=1, or the scope path is rewritten.
az role assignment create --assignee-object-id <object id of the person, group or service principal> \
  --assignee-principal-type <User|Group|ServicePrincipal> \
  --role "Storage Blob Data Contributor" --scope "$SCOPE"
```

**Upload** (and every later update; the uploader signs in with Entra, no key or SAS exists):

```bash
az storage blob upload --account-name "$(out evidenceStorageAccountName)" --container-name contracts --name estate.json \
  --file contracts/estate.json --auth-mode login --overwrite
```

`--overwrite` replaces the current blob by creating a **new version**; the previous version stays readable (`az storage blob list ... --include v`, then `az storage blob download ... --version-id <id>`), so a bad upload is undone by uploading the previous version's content again. Use another `--name` only together with `contractsBlobName`.

**What the app does with it:**

* **At start-up.** A missing or invalid blob leaves the app **not ready**: `/health/ready` answers `503` with the reason `build_failed` and the exception *type* only (for example `ContractsFileNotFoundError` or `ContractsInvalidError`; never its message), and the redacted container log lists every violation as `file:line:column: error: /json/pointer: message`. The build is retried with exponential backoff (`buildRetryInitialSeconds` up to `buildRetryMaxSeconds`); `/health` stays `200`, so there is **no crash loop**. A `rest` deployment therefore succeeds without the file, and the app becomes ready by itself, without a redeployment, within one retry after a valid file is uploaded. That is intended; it also means the smoke test of a first `rest` deployment fails until the upload (docs/delivery.md, section 9, has the `mock`-first route for `prod`).
* **While running.** On every catalog refresh (`catalogRefreshIntervalMinutes`, default `60`: lower it to pick changes up sooner) each replica re-reads the blob, compares its SHA-256 with the contracts in force and, when it changed, validates and applies the new file and writes a `contracts.loaded` audit record with the SHA-256 and the blob version.
* **A bad blob after start.** If the blob becomes missing or invalid, the contracts that were last good **stay in force**. The refresh job reports `FAILED` once (audited when its health changes, not on every attempt) and recovers on its own when a valid blob is uploaded.

The file holds identifiers, names and SLAs, not credentials. Because the account has version-level immutability, every uploaded version is protected for `evidenceRetentionDays` and cannot be deleted before then, so check the file before uploading it. *Verify on the first deployment:* that overwriting `estate.json` while the previous version is under retention creates a new version rather than being refused; the template and this procedure depend on it (if the service refuses, the file needs a new `contractsBlobName` per release).

For a smoke test before Fabric access is in place, deploy once with `fabricMode = 'mock'`. The app then serves the synthetic estate that is baked into the image.

### 5. Grant Fabric and Power BI access (outside ARM/Bicep)

Fabric workspace roles **cannot be assigned from ARM/Bicep**. Grant them after deployment, using the identity principal ids from the outputs. Grant the reader identity read access only. Grant the actor identity write access only where writes are allowed.

1. **Tenant setting (Fabric admin portal > Tenant settings > Developer settings).** Enable **"Service principals can use Fabric APIs"**. Newer tenants split this into *"Service principals can call Fabric public APIs"*, which is the one needed here. Scope it to a security group that contains both identities, rather than the whole organization:
   ```bash
   az ad group create --display-name sg-fabricops-identities --mail-nickname sg-fabricops-identities
   az ad group member add --group sg-fabricops-identities --member-id <readerIdentityPrincipalId>
   az ad group member add --group sg-fabricops-identities --member-id <actorIdentityPrincipalId>
   ```
   The read-only **admin** API settings are **not** needed; FabricOps uses only the non-admin APIs.
2. **Reader identity: workspace `Viewer`** on every workspace FabricOps observes. In the Fabric portal, use **Workspace > Manage access > Add people or groups**, search for `id-<prefix>-reader`, and choose **Viewer**. Or use the Fabric REST API, called by a workspace Admin:
   ```bash
   az rest --method post --resource https://api.fabric.microsoft.com \
     --url "https://api.fabric.microsoft.com/v1/workspaces/<workspaceId>/roleAssignments" \
     --body '{"principal": {"id": "<readerIdentityPrincipalId>", "type": "ServicePrincipal"}, "role": "Viewer"}'
   ```
3. **Power BI semantic model refresh history is not readable with Viewer (observed).** The reader identity with workspace `Viewer` gets **403 `Unauthorized`** on `GET /v1.0/myorg/groups/{groupId}/datasets/{datasetId}/refreshes` (sandbox model, 02:22:51Z to 02:28:18Z on 2026-10-20; `tests/live/test_live_rest.py::test_refresh_history_needs_more_than_viewer`). Its effective dataset right is already `Read` (`GET .../datasets/{id}/users` lists it), so Viewer's read of the model's metadata works, but not this call. What was tried to fix it, and failed: dataset `Read` and `ReadExplore` cannot be assigned to a service principal through the REST API (400 "API supported only for User or Group principal types"), and the Fabric job-instances API answers 404 for a semantic model, even to the owner. Microsoft documents the requirement as write permission on the model ("Caller must have Write permissions on the dataset"), which is what the 403 is consistent with; the programme did not try Contributor because it is a write role, and **no write was granted**. The one read-only route that works is `GET .../datasets/{datasetId}/refreshes/{requestId}` (Get Refresh Execution Details: 200 with Viewer for completed and failed refreshes, with the failure messages), which needs the refresh's request id and cannot list them. Evidence and timestamps: `docs/security.md` section 11. Decision D9 settled it: the reader never gets write, and the refresh outcomes come from **workspace monitoring** instead (item 3a). With `refreshSource = 'workspace_monitoring'` (the default) the Power BI refresh history is not called at all. A deployment that turns `powerbi_api` or `both` on gets that 403 for every model; list `semantic-model-refreshes` in `expectedForbiddenCollectors` so it is reported as *capability unavailable* (a metric and a field on `/health/ready`) and does not keep the collector-errors alert fired.
3a. **Refresh outcomes: workspace monitoring (D9; not yet verified live).** A workspace admin enables *workspace monitoring* in each workspace whose refreshes are to be observed (it creates a monitoring Eventhouse and a KQL database; collection is off until switched on and does not backfill). The reader needs to **query that database**, and Microsoft's documentation contradicts itself about the role: the overview says "at least Contributor", the roles and Kusto pages say workspace Viewer reads every item (`docs/spikes/workspace-monitoring.md`, `docs/security.md` section 11). **Do not grant Contributor**: it is a write role and Admin on every KQL database of the workspace. If a Viewer-only principal is refused, the narrower options are to share the monitoring database with the reader (Read) or to add it as a database viewer; the owner chooses. Leave `workspaceMonitoringDatabase` empty: each workspace's monitoring database is recognised by name (the legacy one is `Monitoring KQL database`; the newer monitoring item names it `<workspace> <item>_Database`, so it differs per workspace). Neither form has been observed on a real monitoring database yet. Set the parameter to one exact display name only if yours follows neither form; two matching databases in a workspace are an error, never a guess.
4. **Capacities and deployment pipelines.** Do not give the reader identity capacity roles or deployment-pipeline Admin. Both carry write permissions: assigning workspaces to a capacity, or deploying. Without them, capacity listing and deployment-state lookups return nothing for that identity. CU utilisation is not available from any public API: it is read from the Fabric Capacity Metrics app's semantic model through `executeQueries` (`capacityMetricsWorkspaceId` and `capacityMetricsDatasetId`), which needs Read **and Build** on that model. A Viewer has no Build, Build cannot be granted to a service principal through the REST API, and Contributor (which has it) is a write role: the access path is an open decision for the owner (`docs/spikes/capacity-metrics.md` lists the options, including ADR-0009's fallback of Real-Time hub capacity events in an Eventhouse). The app's workspace must not be on the capacity it measures.
5. **OneLake `_delta_log` reads (schema-drift collector, ADR-0009).** Workspace `Viewer` gives no OneLake access (every path is 403). The smallest working grant is a *OneLake data access role* with `Read` on one table, created per source lakehouse with `PUT https://api.fabric.microsoft.com/v1/workspaces/{workspaceId}/items/{lakehouseId}/dataAccessRoles` (the call replaces the whole role set, so include the built-in `DefaultReader` role unchanged). Roles exist only at lakehouse-root and table level, so the log cannot be granted without the table's data. It is read-only: other tables, `Files` and every write stay 403. The role body, the proof and the limits are in `docs/security.md` section 11; `scripts/live-estate/estate.sh` (`ensure_table_reader_role`) is a working example. Declare the same tables in the contracts file (`delta_tables`: the lakehouse item id and the table name), because only declared tables are read.
6. **Actor identity: `Contributor` only on workspaces where writes are permitted.** Do this only once a write executor is in use and `enableWriteActions = true`. Use the same portal flow or REST call with `"role": "Contributor"`. Never grant `Admin` or `Member`. Never grant it on workspaces outside the write scope.

**Verify.** As an operator, call `POST /telemetry/collect` and check that `collector_errors` is empty. A 401/403 entry names the API and workspace that still needs a grant. `collector_unavailable` lists what cannot be observed by design (a workspace without monitoring, a documented 403); each name there needs an owner.

> In this MVP the REST adapter is **read-only**. With `fabricMode = 'rest'`, approved actions are executed by `DisabledFabricWriteClient`, which refuses them even when `enableWriteActions = true`. The actor identity and the switch exist so that a future write executor can be enabled without touching the read path. Keeping the two identities separate means a compromised read path holds no write token. The strongest form of that separation is to run the write executor as its own app with only the actor identity attached.

### 6. Azure OpenAI (optional, `reasoner = 'agent_framework'`)

Set `azureOpenAiEndpoint`, `azureOpenAiDeployment` and `azureOpenAiResourceId`. The template grants the reader identity **Cognitive Services OpenAI User** on that account, which permits inference only (no keys, no deployments). The grant is made through `modules/openai-role.bicep`, scoped to the account's own resource group and subscription. The deploying principal needs permission to create role assignments there. Without endpoint and deployment the app refuses to start with `agent_framework`.

## Notifications and the web UI

The app can tell people when an incident opens or escalates, when an approval is requested and when an action ran
([docs/teams-notifications.md](../../docs/teams-notifications.md)), and serves a server-rendered UI under `/ui` (incidents, the
report with FACT / INFERENCE / RECOMMENDATION labels, approvals with approve / reject / execute, audit verification). Both are
[ADR-0018](../../docs/adr/0018-notifications-and-web-ui.md). With the defaults nothing is sent and the UI is served only to callers who
hold a token, exactly like the API.

| Parameter | Default | Purpose |
|---|---|---|
| `notifierKind` | `'null'` | `'null'` (nowhere), `'teams_workflows'` or `'webhook'` (`ORQENTIS_NOTIFIER`) |
| `notifyTeamsWebhookSecretUri` | `''` | Key Vault **secret URI** of the Teams Workflows webhook URL. Required for `teams_workflows` |
| `notifyWebhookSecretUri` | `''` | Key Vault secret URI of the generic webhook URL. Required for `webhook` |
| `notifyWebhookSigningSecretUri` | `''` | Optional Key Vault secret URI of the HMAC-SHA256 key; the receiver verifies `X-Orqentis-Signature` |
| `notifyAllowedHosts` | `''` | Comma-separated host patterns a webhook URL may use. Empty: the Power Platform hosts for Teams, any public host for the generic webhook |
| `publicBaseUrl` | `''` | Origin of the app (`https://fabricops.example.com`, no path). Empty: derived from the environment's default domain. Output `publicBaseUrlEffective` shows the result |
| `uiEnabled` | `true` | Serve `/ui` |
| `uiSignInClientSecretUri` | `''` | Key Vault secret URI of the client secret of the Entra registration; turns on interactive browser sign-in (below) |
| `uiCsrfSecretUri` | `''` | Key Vault secret URI of the HMAC key of the UI form tokens. Required before running more than one replica |

**There is deliberately no parameter that takes a webhook URL or a key.** Each `...SecretUri` is a reference to a Key Vault secret
(a versionless `https://<vault>.<dns-suffix>/secrets/<name>`); Container Apps resolves it with the reader identity into a Container App
secret, and the app receives it as a `secretRef` environment variable (`ORQENTIS_NOTIFY_TEAMS_WEBHOOK_URL`, `ORQENTIS_NOTIFY_WEBHOOK_URL`,
`ORQENTIS_NOTIFY_WEBHOOK_SIGNING_SECRET`, `ORQENTIS_UI_CSRF_SECRET`). With `keyVaultResourceId` set, the reader identity gets
`Key Vault Secrets User` on these secrets and no others (the same module as the cross-tenant credential). The deployment fails with
the reason for: a malformed or versioned URI, a URI that is not in the vault named by `keyVaultResourceId`, `teams_workflows` without
`notifyTeamsWebhookSecretUri`, `webhook` without `notifyWebhookSecretUri`, and a `publicBaseUrl` that is not an https origin. A parameter file
that contains a value instead of a reference is refused by the credential guard test, like any other credential-looking parameter.

### Interactive sign-in for the UI

The API only validates bearer tokens, which a browser cannot send. For people to open `/ui` (and the links in notifications) the
registration from step 1 also signs them in through Easy Auth's `/.auth/login/aad`, and the browser session carries an ID token:
the app classifies it as a person (`ADR-0015`, row 8), with the roles assigned to them, and the UI applies the same checks as the API.

**Why a client secret and not a managed-identity federated credential.** Easy Auth can authenticate the app to Entra with a
federated identity credential that trusts a managed identity, so no secret has to be created or rotated. That is documented for App
Service (a preview feature: a federated credential on the app registration, the app setting `OVERRIDE_USE_MI_FIC_ASSERTION_CLIENTID`, and
`clientSecretSettingName` pointing at it). **It is not documented for Container Apps**: the Container Apps article on the Microsoft Entra
provider (updated 2026-02-20) lists only a client secret, and the open issue
[microsoft/azure-container-apps#1693](https://github.com/microsoft/azure-container-apps/issues/1693) (2026-04-13) reports that the
Container Apps sidecar treats the sentinel as a literal secret and sign-in fails with `AADSTS7000215`, with an unconfirmed report that a lower-case
secret name works. The template therefore uses a **client secret that exists only in Key Vault** (`uiSignInClientSecretUri`), with the authorization
code flow, no token store, an 8 hour session and no redirect to another site. Switch to the federated credential when Container Apps supports it; nothing
else changes. The secret expires (Entra allows at most 24 months): note the date, rotate it in Key Vault and restart the revision.

1. **Register the redirect URI and allow ID tokens** on the registration from step 1. The redirect URI is the `entraRedirectUri` output of a
   deployment (`<publicBaseUrl>/.auth/login/aad/callback`), or derive it from `publicBaseUrl`:
   ```bash
   az ad app update --id "$API_APP_ID" --web-redirect-uris "https://<app origin>/.auth/login/aad/callback" --enable-id-token-issuance true
   ```
2. **Create the client secret and put it straight into Key Vault** (never in a file, parameter or pipeline variable):
   ```bash
   SECRET=$(az ad app credential reset --id "$API_APP_ID" --append --display-name "fabricops-ui-signin" --years 1 --query password -o tsv)
   az keyvault secret set --vault-name <vault> --name fabricops-ui-client-secret --value "$SECRET" --output none
   unset SECRET
   ```
3. **Set** `uiSignInClientSecretUri = 'https://<vault>.vault.azure.net/secrets/fabricops-ui-client-secret'` (and `keyVaultResourceId`) and deploy.
   If you use `allowedClientApplications`, the template adds the registration's own client id to it, because the browser session is a
   request from that application.
4. **Sign in** at `https://<app origin>/.auth/login/aad?post_login_redirect_uri=/ui`. A request without a session still gets `401`, as
   before (`Return401`): there is no automatic redirect to a login page, because anonymous API calls and the CD smoke test rely on
   the 401. Notification links already go through the sign-in endpoint. Sign out at `/.auth/logout`.
5. **Check:** the UI shows your name and your roles at the top. A person with no FabricOps role gets a 403 page, an application token sees the
   pages but no decision buttons, and a decision from a foreign origin or without the form token is refused (see
   [docs/security.md](../../docs/security.md), *Web UI and notifications*).

Assign the app roles to people as in step 1 (`FabricOps.Reader` to read, `FabricOps.Operator` / `FabricOps.PlatformAdmin` to decide).
The roles reach the UI in the ID token's `roles` claim; if they do not, the registration's app roles are not assigned to the person or the
group (**Enterprise applications > Users and groups**).

**Several replicas.** The form tokens are signed with a per-process random key unless `uiCsrfSecretUri` is set. Set it (any long random value in
Key Vault) before `maxReplicas` is raised, or a form rendered by one replica is refused by another. Also note that the notification outbox is in
memory and the rate limits are per replica ([ADR-0018](../../docs/adr/0018-notifications-and-web-ui.md)).

## Cross-tenant Fabric access (service principal + Key Vault)

Use this when the Fabric tenant is a **different Entra tenant** from the one that hosts this deployment. A managed identity exists only in the tenant that hosts it and cannot be granted access to another tenant's Fabric, so FabricOps signs in as a **service principal registered in the Fabric tenant** (`ORQENTIS_AUTH_MODE=service_principal`). Its secret or certificate is a **Key Vault reference** that Container Apps resolves with the reader managed identity. No credential value is ever a Bicep parameter, an output, a CI variable or a file in the repository. An example parameter file is [`examples/cross-tenant.bicepparam`](examples/cross-tenant.bicepparam).

```text
 Azure tenant A (hosts FabricOps)                       Fabric tenant B
 ┌───────────────────────────────────────────┐          ┌────────────────────────────────┐
 │ Container App ── reader managed identity ─┼─ reads ─▶│ (Key Vault in tenant A)        │
 │   AZURE_CLIENT_SECRET / cert file ◀───────┘          │                                │
 │   Azure OpenAI ◀── reader managed identity           │ service principal (read-only)  │
 │   Fabric / Power BI ── service principal token ─────▶│ Fabric workspaces: Viewer      │
 └───────────────────────────────────────────┘          └────────────────────────────────┘
```

1. **Register the service principal in the Fabric tenant** (a Fabric tenant administrator, or someone who can create app registrations there). Single-tenant is enough: it signs in to its own tenant.
   ```bash
   az login --tenant <fabric-tenant-id>
   FABRIC_SP_APP_ID=$(az ad app create --display-name "Orqentis FabricOps reader" --sign-in-audience AzureADMyOrg --query appId -o tsv)
   az ad sp create --id "$FABRIC_SP_APP_ID"
   ```
2. **Create its credential and put it straight into a Key Vault.** The vault belongs to the Azure tenant that hosts FabricOps, must use **Azure RBAC**, and must be reachable by Container Apps (public access, or a trusted-services exception, or a private endpoint in the VNet). Choose one:
   * **Client secret.** Create it in the portal (App registrations > Certificates & secrets) or with `az ad app credential reset`, and store the value in a Key Vault secret such as `fabric-sp-secret`. Do not paste it into a parameter file, a pipeline variable or a shell command line. Set a short expiry and rotate it.
   * **Certificate (preferred: no shared secret to leak).** Create a Key Vault certificate whose **content type is PEM** (`application/x-pem-file`), so that the Key Vault *secret* with the same name holds the certificate and its private key as PEM, which is what `AZURE_CLIENT_CERTIFICATE_PATH` expects. Upload only the **public** certificate to the app registration (App registrations > Certificates & secrets > Certificates). A PKCS#12 certificate (the Key Vault default) is not readable by the app.
3. **Grant the service principal Fabric read access in the Fabric tenant**, exactly as for the managed identity in step 5 above: enable *Service principals can use Fabric APIs* for a security group that contains it, and give it workspace **Viewer** on every workspace FabricOps observes. Use the service principal's own object id (`az ad sp show --id "$FABRIC_SP_APP_ID" --query id -o tsv`), not the managed identity's. It needs no admin API permission and no write role.
4. **Fill the parameters** (copy `examples/cross-tenant.bicepparam`):

   | Parameter | Value |
   |---|---|
   | `fabricAuthMode` | `service_principal` |
   | `fabricTenantId` | The **Fabric** tenant id (a GUID; `ORQENTIS_TENANT_ID` and `AZURE_TENANT_ID`). Required |
   | `fabricServicePrincipalClientId` | The app registration's application (client) id. Not a secret |
   | `fabricServicePrincipalSecretUri` **or** `fabricServicePrincipalCertificateUri` | `https://<vault>.vault.azure.net/secrets/<name>` (no version, so a new version is picked up). Set exactly one |
   | `keyVaultResourceId` | Optional. Full resource id of the vault; the template then grants the reader identity `Key Vault Secrets User` **on the referenced secret(s) only**, so a compromised reader identity cannot read anything else in the vault. Leave empty and grant it yourself if the vault uses another model |
   | `entraClientId`, `entraTenantId` | Unchanged: they protect the **FabricOps API** in the Azure tenant, whoever your approvers are |

   The template checks these inputs when the deployment runs and fails with the reason instead of deploying a container that crash-loops: no client id, no explicit `fabricTenantId` (a GUID), not exactly one of the two URIs, a URI that is not a Key Vault secret (`https://<vault>.<dns-suffix>/secrets/<name>`), or a URI in a different vault from `keyVaultResourceId`. The Key Vault role is assigned on those secrets only.

   Container Apps resolves the Key Vault reference when a revision starts, so a Key Vault role assignment made in the same deployment can take a few minutes to take effect. If the first deployment fails with a Key Vault access error, wait and re-run it.
5. **What the app receives.** `ORQENTIS_AUTH_MODE=service_principal`, `ORQENTIS_TENANT_ID` and `AZURE_TENANT_ID` (Fabric tenant), `AZURE_CLIENT_ID` (service principal), and either `AZURE_CLIENT_SECRET` (a secret reference) or `AZURE_CLIENT_CERTIFICATE_PATH` (a read-only file mounted from the secret at `/mnt/fabric-sp/service-principal.pem`; the private key is never written to a disk). **Azure OpenAI keeps using the reader managed identity** (`ORQENTIS_LLM_AUTH_MODE=managed_identity`), because the model lives in the Azure tenant, not the Fabric one. If the credential is missing, or both a secret and a certificate are set, the app refuses to start and names the missing variables (never their values).
6. **Rotate.** Add a new version of the Key Vault secret (or certificate), then restart the revision (`az containerapp revision restart`): environment variables are read when the container starts. Remove the old credential from the app registration afterwards.

A keyless alternative, a *federated identity credential* on the app registration that trusts the reader managed identity, would remove the secret and its rotation. It needs a different credential type in the app and is tracked as a follow-up in ADR-0014.

## Sovereign clouds

The Entra authority host and the Fabric and Power BI token scopes are settings (and Bicep parameters), so a deployment into another cloud needs no code change. Leaving them empty keeps the public cloud. The endpoint sets that Microsoft documents for **Power BI** are:

| Cloud | `authorityHost` | `powerbiApiBaseUrl` | `powerbiScope` |
|---|---|---|---|
| Public (default) | `https://login.microsoftonline.com` | `https://api.powerbi.com/v1.0/myorg` | `https://analysis.windows.net/powerbi/api/.default` |
| US Government, GCC | `https://login.microsoftonline.com` | `https://api.powerbigov.us/v1.0/myorg` | `https://analysis.usgovcloudapi.net/powerbi/api/.default` |
| US Government, GCC High | `https://login.microsoftonline.us` | `https://api.high.powerbigov.us/v1.0/myorg` | `https://high.analysis.usgovcloudapi.net/powerbi/api/.default` |
| US Department of Defense | `https://login.microsoftonline.us` | `https://api.mil.powerbigov.us/v1.0/myorg` | `https://mil.analysis.usgovcloudapi.net/powerbi/api/.default` |
| China (21Vianet) | `https://login.chinacloudapi.cn` | `https://api.powerbi.cn/v1.0/myorg` | `https://analysis.chinacloudapi.cn/powerbi/api/.default` |

Sources: Microsoft Learn, [Power BI for US Government customers](https://learn.microsoft.com/en-us/fabric/enterprise/powerbi/service-government-us-overview) and [embedding for national/regional clouds](https://learn.microsoft.com/en-us/power-bi/developer/embedded/embed-sample-for-customers-national-clouds). The `/v1.0/myorg` path is the same REST API path as in the public cloud.

**Fabric availability differs by cloud, and the Fabric REST API base URL and scope for sovereign clouds are not in the sources used here.** Take `fabricApiBaseUrl` and `fabricScope` from Microsoft's documentation for your cloud and tenant, and confirm that Fabric itself is available there before relying on any of this: for example, Microsoft documents that F SKU (Azure Embedded) capacities are not supported in GCC but are in GCC High and DoD. The Azure OpenAI scope in a sovereign cloud differs too (it is chosen by the model client, not by these settings). FabricOps has been exercised only against recorded responses and fake Entra endpoints; no sovereign cloud was contacted. The unit tests prove that the configured authority host reaches the credentials and that the configured scopes are the ones requested.

## Time zone

`defaultTimezone` (default `Australia/Sydney`, which keeps earlier deployments unchanged) becomes `ORQENTIS_DEFAULT_TIMEZONE`. It is used for time-of-day reasoning (for example whether a failure is in a business-hours window) and report times. The app validates it at startup and refuses an unknown zone with a message that names the setting. SLAs carry their own `timezone` in the contracts file.

## Network exposure

`ingressMode` selects who can reach the API. **Authentication is on in every mode**: Container Apps authentication returns 401 for a request without a valid Entra ID token for this API, and the app then checks the caller's roles.

| `ingressMode` | `infrastructureSubnetId` | Container App `ingress.external` | Who can reach the API |
|---|---|---|---|
| `externalAuthenticated` (default) | optional | `true` | Anyone on the internet **who presents a valid token**. Add `allowedIpRanges` to limit by address before authentication. With a subnet the environment is VNet-integrated (its outbound traffic leaves through the VNet, which is how the app reaches a private PostgreSQL server) but keeps this public endpoint. `test` and `prod` use it this way. |
| `vnet` | required | `true` in an internal environment | Only the VNet (no public IP): peered networks, VPN, ExpressRoute. Front it with Application Gateway/APIM or create a private DNS zone for the environment's default domain. |
| `internal` | optional | `false` | Only other apps in the same Container Apps environment. People cannot reach it. |

The subnet must be delegated to `Microsoft.App/environments` and be at least `/27`, because the environment uses workload profiles. `vnet` without a subnet feeds an invalid subnet id into the environment, so the deployment **fails** instead of silently exposing the API differently from what was asked. (`externalAuthenticated` with a subnet used to fail too; it is valid now: the environment is VNet-integrated and the endpoint stays public and authenticated.) Ready-made parameter files are in [`examples/`](examples/), one per mode (plus the deprecated `externalIngress` override). Build them with the loop under *Validate locally*; CI currently builds `main.bicepparam` only.

**Why external and authenticated is the default.** The previous default (`externalIngress = false`) left the API reachable only from inside the environment, so people could not use it and the approval workflow was unusable out of the box. Exposure is defence in depth, not the access control: the access control is the Entra ID token (audience, issuer and lifetime validated by Container Apps, only users and applications assigned to the app can obtain one, `appRoleAssignmentRequired=true`) plus the role checks and human-only approval in the app. A public address that answers 401 to everyone else is acceptable for that control set. Tighten it where the threat model needs more: `allowedIpRanges` for a known office or runner range, `vnet` for private-only access. **This changes the default for deployments that never set `externalIngress`**: on the next deployment they become externally reachable (authenticated). Deployments that set it explicitly keep their behaviour, see below.

**Migrating from `externalIngress`.** The parameter is deprecated but still honoured when `ingressMode` is not set, so existing parameter files keep their behaviour: `false` is `internal`, `true` is `externalAuthenticated` (or `vnet` when `infrastructureSubnetId` is set). **Setting both `externalIngress` and `ingressMode` fails the deployment**, so a stale `externalIngress = true` cannot silently override an explicit `ingressMode = 'internal'`. Remove `externalIngress` from your parameter files and set `ingressMode`:

| Before | After |
|---|---|
| `externalIngress = false` (or unset) | `ingressMode = 'internal'` |
| `externalIngress = true`, no subnet | `ingressMode = 'externalAuthenticated'` |
| `externalIngress = true`, `infrastructureSubnetId` set | `ingressMode = 'vnet'` |
| `externalIngress = false`, `infrastructureSubnetId` set | `ingressMode = 'internal'` (still VNet-integrated) |

**First deployment and the Easy Auth window.** The app trusts the `X-MS-CLIENT-PRINCIPAL` header and does not validate bearer tokens itself; Container Apps authentication is the only thing that does. While an externally reachable app has no active authentication, anyone can send a forged header that names a `FabricOps.PlatformAdmin` and the app will believe it. That can happen in the first deployment (the container app exists before its child `authConfigs` resource is applied), after someone disables authentication (`az containerapp auth update --enabled false`), or after a platform fault. So deploy safely:

1. **First deployment with `ingressMode = 'internal'`** (nothing outside the environment can connect), `fabricMode = 'mock'` if you have no contracts file yet. Also complete the PostgreSQL bootstrap (section 3a) before expecting the app to become ready.
2. **Verify authentication is on:** `az containerapp auth show -g "$RG" -n ca-<prefix>` must show `platform.enabled: true` and `globalValidation.unauthenticatedClientAction: Return401`.
3. **Then switch** to `externalAuthenticated` (or `vnet`) and redeploy. Check from outside: `curl -i https://<fqdn>/incidents` must answer **401**, and so must the same request with a forged header, for example `-H "X-MS-CLIENT-PRINCIPAL: $(echo -n '{"claims":[]}' | base64)"` (Container Apps strips client-supplied copies). Make these two checks part of every deployment pipeline.
4. Restrict who can disable authentication (RBAC on the container app), and alert on `Microsoft.App/containerApps/authConfigs` writes in the Activity Log.

Residual risk, recorded in ADR-0015 and the threat model: the app does not validate bearer tokens itself, so an exposed app without Easy Auth is fully trusting. Validating the token in the app (signature, issuer, audience) is a follow-up. `allowedClientApplications` and `allowedIpRanges` narrow who can reach the API in the meantime, and `vnet` or `internal` remove the exposure.

**Restricting client applications.** `allowedClientApplications` is a list of client application ids that Easy Auth lets through (it checks the token's `azp`/`appid`). Empty (default) means any application can call with a valid token for this API, for example a third-party app that a user consented to. **Recommended in production:** list the clients people use (the UI, Azure CLI `04b07795-8ddb-461a-bbee-02f9e1bf7b46` for operator testing) and the client ids of the automation identities (a managed identity's client id is its application id). A caller whose application is not listed gets 403 from Easy Auth before the app sees the request.

**Health endpoints.** By default (`anonymousHealthEndpoints = true`) `/health` and `/health/ready` are excluded from authentication, so uptime checks and smoke tests can call them without a token. `/health` returns the version, `fabric_mode`, `reasoner` and the time, and reads no caller identity. It does not reveal whether writes are enabled: that flag is on `GET /system/info`, which needs a FabricOps role. Container Apps documents `excludedPaths` as "the paths for which unauthenticated flow would not be redirected to the login page" and does not say whether the match is exact or by prefix, so the app does not rely on it: every other route requires the identity header and returns 401 without it, and no route under `/health` reads it. Set `anonymousHealthEndpoints = false` if version and mode disclosure to the internet matters more than anonymous probes (container health probes are unaffected, they bypass authentication). Smoke tests can then authenticate with an identity holding `FabricOps.Automation`. `/health/ready` answers `200`, or `503` with a fixed reason code (`starting` or `build_failed`) and the exception *type* of the last build failure, never its message (paths and other detail stay in the logs), so anonymous callers learn nothing about the deployment from it.

**Trust boundary.** With `ORQENTIS_API_AUTH_MODE=easy_auth` the app trusts the `X-MS-CLIENT-PRINCIPAL` header. Container Apps authentication sets that header and strips any client-supplied copy, so the app must only be reached through Container Apps ingress. Do not expose additional ports. Keep the environment dedicated to FabricOps, without other or untrusted workloads. Health probes target the container port directly and are not subject to authentication.

**Request limits.** The app rejects bodies over `apiMaxBodyBytes` with 413 and rate-limits each **authenticated** principal with 429 and `Retry-After`: approve, reject and execute (`apiRateLimitMutatingPerMinute`) and the expensive calls, telemetry collection, investigations and audit verification (`apiRateLimitExpensivePerMinute`). The budget is charged after authentication on the validated principal id, so a forged header on `/health` cannot spend anyone's budget. The limits live in memory **per replica**, so with N replicas one principal can make up to N times the configured number of calls per minute (`test` and `prod` run 2). Reads are not limited by the app. Put Azure Front Door or API Management in front if the API needs a global limit, WAF rules or request logging at the edge.

## Private networking (test and prod)

Outside `dev` the template **refuses to deploy** (a readable failure) unless the database is private: `test` and `prod` must set `infrastructureSubnetId`, `postgresDelegatedSubnetId` and `postgresPrivateDnsZoneId`, and must not set `postgresAllowAzureServices`. Public access with a firewall is for `dev` only. The "Allow Azure services" rule opens PostgreSQL's port to resources of **every Azure tenant**; every connection would still need a valid Entra token for an existing role, but with a private path available there is no reason to leave the port open.

```text
 VNet (one per environment, or a spoke of a hub)
 ├─ snet-apps      delegated to Microsoft.App/environments          the Container Apps environment (infrastructureSubnetId)
 └─ snet-postgres  delegated to Microsoft.DBforPostgreSQL/flexibleServers   the PostgreSQL server (postgresDelegatedSubnetId)
 private DNS zone  <name>.private.postgres.database.azure.com, linked to the VNet (postgresPrivateDnsZoneId)
```

The Container Apps environment is integrated with the VNet, so the app's outbound traffic leaves through `snet-apps` and reaches the server's private address, which the server's name resolves to through the private DNS zone. The API keeps its public, authenticated endpoint (`ingressMode = 'externalAuthenticated'`: every request still needs an Entra token), because the GitHub-hosted smoke test and the people who use the API reach it from the internet. A fully private API (`ingressMode = 'vnet'`) is a separate decision that also moves the smoke test inside the network (*Network exposure*).

**Why VNet integration and not a private endpoint.** Flexible Server can be made private in two ways. VNet integration (a delegated subnet plus a private DNS zone) is the long-established mode and is available on every compute tier, including the Burstable `Standard_B1ms` that `dev` and `test` use; the module already implements it; it needs no further resources (no endpoint, no network interface, no `privatelink` zone group); and the server then has no public endpoint at all, so there is no switch that could turn one back on. A private endpoint adds a network interface and its own DNS wiring, and earns its keep when one server must be reached from many VNets without peering, which this deployment (one VNet per environment) does not need. The costs of VNet integration: the mode is fixed when the server is created (an existing public server cannot be converted, so `test` and `prod` get new servers), the delegated subnet holds nothing but that server, and the subnets and the server must be in one region. I could not verify offline whether private endpoints are available for the Burstable tier.

**What the owner provisions before the first `test` or `prod` deployment** (the template creates none of it, and the deployment fails without the ids):

1. A **VNet** in the environment's region.
2. `snet-apps`: delegated to `Microsoft.App/environments`, at least `/27` (the environment uses workload profiles), with nothing else in it.
3. `snet-postgres`: delegated to `Microsoft.DBforPostgreSQL/flexibleServers`, at least `/28` (check the current minimum), with nothing else in it.
4. A **private DNS zone** whose name ends in `postgres.database.azure.com` (for example `fabricops-test.private.postgres.database.azure.com`), with a **virtual network link** to the VNet, and to any VNet an operator connects from.
5. Rights for the environment's deploy identity (`fabricops-deploy-<env>`), granted by the owner outside the template (and outside the constrained role-assignment condition): `Network Contributor` on the VNet, or at least the `Microsoft.Network/virtualNetworks/subnets/join/action` on the two subnets, and `Private DNS Zone Contributor` on the zone. Without them the environment or the server cannot attach to the subnets. *(Not verified offline: if the first deployment reports an authorization failure on `subnets/join` or on the zone, grant exactly the action it names.)*
6. The three **GitHub environment variables** `CONTAINER_APPS_SUBNET_ID`, `POSTGRES_DELEGATED_SUBNET_ID` and `POSTGRES_PRIVATE_DNS_ZONE_ID` (resource ids), set for `test` and `prod` ([docs/delivery.md](../../docs/delivery.md), section 7). `bash scripts/validate-bicep.sh` and `scripts/bicep-snapshot.sh` use fake ids of the same shape.
7. **A way in for people.** The app needs nothing more (step 1 to 4; if you put network security groups on the subnets they must allow TCP 5432 from `snet-apps` to `snet-postgres`). The one-off bootstrap (section 3a), the restore checks ([docs/operations/restore.md](../../docs/operations/restore.md)) and `orqentis db status` / `orqentis audit` from a workstation need a client inside the network: a small Linux VM in the VNet (or a peered VNet), reached through Azure Bastion or a VPN, with the Azure CLI, `psql` 16 or later and a checkout of the repository; or a self-hosted runner there. Management commands that only use Azure's control plane (`az postgres flexible-server restore`, `show`, `update`) work from anywhere. `az containerapp exec` opens a shell in a replica, which is inside the network and holds the app's identity, for `orqentis db status` and `orqentis audit`, but not for the bootstrap, which needs an administrator. The contracts upload and reading evidence use the storage account's public endpoint with Entra, and need no network path.

`dev` keeps public access: `postgresAllowAzureServices = true` (the only environment where the template accepts it) and, for whoever runs the bootstrap, `postgresAllowedIpRanges`. A server restored in a private environment needs `--vnet`, `--subnet` and `--private-dns-zone` on the restore command, or the restored server is not private.

## Storage

A deployed environment keeps **no state in the container**. Two stateful services hold it, both reached with the app's managed identity and no secret:

| What | Where | Why |
|---|---|---|
| Incidents, approvals, dependencies, operational events with collector watermarks, scheduler leases, investigation jobs, the audit trail (records and chain head) | PostgreSQL Flexible Server `psql-<prefix>-<hash>`, database `orqentis` (`modules/postgres.bicep`) | Transactions, row locks and compare-and-set make concurrent replicas safe; backups with point-in-time restore |
| Evidence artefacts and incident reports | Blob container `evidence` (`ORQENTIS_EVIDENCE_CONTAINER`) | Write-once; version-level immutability |
| Audit-chain anchors | Blob container `audit-anchors` (`ORQENTIS_AUDIT_ANCHOR_CONTAINER`) | The chain head (sequence and hash), copied periodically to storage the database cannot rewrite |
| The estate contracts file | Blob container `contracts`, blob `estate.json` (`ORQENTIS_CONTRACTS_CONTAINER`, `ORQENTIS_CONTRACTS_BLOB_NAME`) | Read-only for the app; versioned, soft-deleted and immutable per version like the other containers; re-read on the catalog refresh |
| `ORQENTIS_DATA_DIR` (`/var/lib/orqentis`) | The container's own disk | Ephemeral and per replica: scratch space, lost when the replica restarts. Nothing durable is written there |

There is no Azure Files share, no SQLite file and no storage account key in a deployed environment. `ORQENTIS_STORAGE_BACKEND=postgres` is set by the template, and the app refuses to start on local files in `dev`, `test` or `prod`. SQLite, the file evidence store and the JSONL audit log remain the adapters for local development, tests and `make demo` ([ADR-0003](../../docs/adr/0003-local-stores-first.md)).

### PostgreSQL

* **Entra only.** `authConfig` has `activeDirectoryAuth: Enabled` and `passwordAuth: Disabled`: the server has no administrator login and no password. A connection presents an Entra access token for `https://ossrdbms-aad.database.windows.net` as the password. The app uses the reader managed identity (the database role is named after it, `id-<prefix>-reader`); people use their own token. TLS is required (`require_secure_transport` is `ON`).
* **Administrator.** `postgresEntraAdminObjectId` and `postgresEntraAdminName` are **required**; `postgresEntraAdminType` is `Group` (default), `User` or `ServicePrincipal`. Use a group, so people can be added and removed without touching the server. Its members run the bootstrap step (section 3a). The pipeline reads the values from the `POSTGRES_ENTRA_ADMIN_OBJECT_ID`, `POSTGRES_ENTRA_ADMIN_NAME` and `POSTGRES_ENTRA_ADMIN_TYPE` variables ([docs/delivery.md](../../docs/delivery.md)). Setting the administrator needs no Azure role beyond the deploy identity's `Contributor` on the resource group.
* **Sizing.** `postgresSkuName` (default `Standard_B1ms`, Burstable; `prod` uses `Standard_D2ds_v5`; the tier is derived from the name), `postgresStorageSizeGiB` (default `32`; storage auto-grows, and once it has grown, raise the parameter to the current size before the next deployment because a smaller value is refused), `postgresVersion` (default `17`; applied when the server is created), `postgresBackupRetentionDays` (default `7`, up to `35`), `postgresGeoRedundantBackup` (default `false`; fixed when the server is created, it copies backups to the paired region, which is a data-residency decision), `postgresHighAvailabilityMode` (`Disabled`, `ZoneRedundant` or `SameZone`; not on the Burstable tier, and the deployment fails if you combine them) and `postgresDatabaseName` (default `orqentis`).
* **Connection budget.** Every replica opens up to `postgresPoolMax` connections (`ORQENTIS_POSTGRES_POOL_MAX`, default `6`, at least `2`: the schema migration holds one connection under its lock). Plan for the worst moment of a deploy: the old revision and the new one both running at `maxReplicas` (the new revision scales up while the old one drains). The demand is therefore `2 x maxReplicas x postgresPoolMax`, plus about 15 connections the platform reserves, plus 5 for the migration and for operators (`psql`, the restore checks). `Standard_B1ms` allows about 50 (`max_connections`): `test`'s 2 replicas x 6 gives 2 x 2 x 6 + 20 = 44, which fits; with a pool of 8 it would be 52, and the deployment **fails** on `Standard_B1ms` with the numbers when the plan needs more than 50. Other SKUs are not checked by the template; read the limit with `SHOW max_connections;` (it grows with the SKU).
* **Network: private outside dev.** `test` and `prod` use VNet integration: `postgresDelegatedSubnetId` (a subnet delegated to `Microsoft.DBforPostgreSQL/flexibleServers`, not the Container Apps subnet) together with `postgresPrivateDnsZoneId`, and a VNet-integrated Container Apps environment (`infrastructureSubnetId`). The server then has no public endpoint and firewall rules cannot be combined with it. The design, the reason for this choice and what must exist beforehand are under *Private networking*; [`examples/ingress-vnet.bicepparam`](examples/ingress-vnet.bicepparam) and `main.bicepparam` show the parameters.
* **Network: public access, dev only.** `postgresAllowedIpRanges` is an array of `{ name, startIpAddress, endIpAddress }` rules (empty by default: nobody outside Azure can connect; add the address of whoever runs the bootstrap step), and `postgresAllowAzureServices` (template default `false`; **only `dev` sets `true`, and the deployment fails anywhere else**) adds the documented `0.0.0.0` rule that lets Azure services connect, which is what lets Container Apps without a VNet reach the server. It opens the TCP port to resources of every Azure tenant; every connection still needs a valid Entra token for an existing database role.
* **Use an existing server.** `postgresExistingServerName` is empty by default. Set it to the name of a server in the same resource group, for example one restored from a backup, and the template creates nothing: it points the app at that server and leaves it alone (its Entra administrator, firewall rules and parameters are then yours to keep). [docs/operations/restore.md](../../docs/operations/restore.md) uses it.
* Encryption at rest uses the platform's service-managed keys; customer-managed keys are not configured.

### Evidence and audit anchors (Blob)

`modules/evidence-storage.bicep` creates a StorageV2 account (`stev<prefix><hash>`) with `allowSharedKeyAccess: false` (no account key is in use: not for the app, the pipeline or the portal; use `--auth-mode login`), Entra as the default authentication, TLS 1.2, HTTPS only, no public blob access, no cross-tenant replication and **no hierarchical namespace**: it speaks the plain Blob API, which is also what the Azurite emulator speaks. The same adapter can write to a OneLake item instead of this account by setting `ORQENTIS_EVIDENCE_BLOB_URL` (the OneLake blob endpoint), `ORQENTIS_EVIDENCE_CONTAINER` (the workspace) and `ORQENTIS_EVIDENCE_PATH_PREFIX` (the item folder); the template deploys the account either way.

* **Immutability.** Version-level immutability is enabled for the account with a default retention policy of `evidenceRetentionDays` (default `30`, up to `3650`) that every new blob version inherits: a version cannot be changed or deleted until its retention ends. The setting can only be switched on when an account is created, which is why this account has its own name (`stev...`, not the `st...` account of earlier template versions). An overwrite creates a new version and the previous one stays protected. *Verify on the first deployment:* reports that are re-rendered are written as overwrites; if the service refuses an overwrite of a blob whose current version is under retention, reports must be written under a new name per render instead.
* **`Unlocked` or `Locked`: the owner's decision.** `evidenceImmutabilityState` is `Unlocked` or `Locked`, and in the environment files it comes from one pipeline variable, `EVIDENCE_IMMUTABILITY_LOCKED` (`true` maps to `Locked`, `false` to `Unlocked`, anything else fails the build with a message; this mapping is the only place that sets `Locked`). In `prod` the variable is **required, with no default**, so the policy is never locked or left open by omission; in `dev` and `test` it is optional and defaults to `false`. **`Locked` is irreversible for the retention period:** the retention can then only be extended, never shortened or removed, and the account cannot be deleted until every blob's retention has expired. It needs the owner's sign-off. **While the policy is `Unlocked`, anyone holding `Owner` or `Storage Account Contributor` (or any role with management rights) on the account can change or remove the policy and then delete anchors, audit epoch markers and evidence**, so keep those roles minimal until it is locked (the deploy identity's `Contributor` on the resource group is one of them). To lock `prod`: deploy once with `EVIDENCE_IMMUTABILITY_LOCKED=false` (a policy cannot be created `Locked`; the platform allows only the Unlocked to Locked transition), confirm `evidenceRetentionDays` (the proposed 365 is the owner's to confirm), get the sign-off, set the variable to `true` and deploy again. Lowering `evidenceRetentionDays` is refused once locked.
* **Soft delete and versioning.** Versioning is on. Blob soft delete and container soft delete keep a deleted or overwritten blob, and a deleted container, recoverable for `evidenceSoftDeleteDays` (default `14`). Change feed is off. Recover with `az storage blob undelete` and `az storage container restore`, and list versions with `az storage blob list --include v`.
* **Who may write where.** Always at container scope, never a role on the account, and no keys or SAS exist:

  | Principal | `evidence` | `audit-anchors` | `audit-epochs` | `contracts` | Storage account (management plane) |
  |---|---|---|---|---|---|
  | The app (reader identity) | read and write | read and write | **read only** | **read only** | nothing |
  | The actor (WRITE) identity | nothing | nothing | nothing | nothing | nothing |
  | Contracts uploaders (people or identities the owner chooses) | nothing | nothing | nothing | write (`Storage Blob Data Contributor`) | nothing |
  | Audit operators group (`auditOperatorsPrincipalId`, optional) | nothing | nothing | write (`Storage Blob Data Contributor`) | nothing | nothing |
  | The environment's deploy identity | nothing (no data role) | nothing | nothing | nothing | **`Contributor` on the resource group: the only identity that can change the account, and so the immutability policy while it is `Unlocked`** |

  The people who hold `Owner` or `Contributor` on the resource group or the subscription are the owner's own decision. The app's role assignments (`Storage Blob Data Contributor` on two containers, `Storage Blob Data Reader` on two) contain no management action: the reader identity cannot change a policy, a key or a network rule. It cannot list or create containers either (the template creates them). The template assigns **no uploader** and, when `auditOperatorsPrincipalId` is empty, **no operator**: the owner grants them, for example for uploaders

  ```bash
  az role assignment create --assignee-object-id <object id> --assignee-principal-type <User|Group|ServicePrincipal> \
    --role "Storage Blob Data Contributor" --scope "<storage account id>/blobServices/default/containers/contracts"
  ```

  and for audit operators the same with `/containers/audit-epochs` (or set `auditOperatorsPrincipalId`, from the pipeline variable `AUDIT_OPERATORS_OBJECT_ID`, so that the template assigns it). People who need to read evidence or anchors need a data role of their own, for example `Storage Blob Data Reader` on a container.
* **Audit epoch markers.** After a restore the audit history is cut and a new epoch starts (`orqentis audit start-epoch`, [docs/operations/restore.md](../../docs/operations/restore.md)). The marker that says so lives in `audit-epochs`, a container the app can only **read**: a compromised app identity can neither open an epoch (and so reset what verification checks) nor remove a marker. Only the operators group can write there, and they also need the database right to act as the application role (section 3a, `operator_role`).
* **Network.** `evidenceNetworkDefaultAction` is `Allow` by default, in every environment: the public endpoint answers any caller, and every request still needs an Entra token and a role (a private endpoint or a `Deny` rule with the Container Apps subnet's `Microsoft.Storage` service endpoint is a possible hardening, not part of the PostgreSQL decision). `Deny` with `evidenceAllowedIpRanges` and/or `evidenceAllowedSubnetIds` restricts it (trusted Azure services stay allowed). An environment without a VNet has no fixed address, so `Deny` is for ingress mode `vnet` with a subnet that has the `Microsoft.Storage` service endpoint; the deployment fails if `Deny` has neither a range nor a subnet.
* **Redundancy.** `storageSkuName`: `Standard_LRS` (`dev`, `test`) or `Standard_ZRS` (`prod`).

### Backups and restore

PostgreSQL backs itself up continuously; `postgresBackupRetentionDays` (7 to 35) is how far back a point-in-time restore can go. A restore creates a **new** server. [docs/operations/restore.md](../../docs/operations/restore.md) is the runbook, with the exact commands, what happens to evidence and the audit anchor, and the rehearsal checklist the owner signs off. Evidence and anchors are protected by versioning, soft delete and immutability, not by the database backup.

### Upgrading from the Azure Files template

This release changes the interface of the template:

* `fileShareQuotaGiB` is removed (a parameter file that still sets it no longer compiles), and so are the `storageAccountName` and `dataFileShareName` outputs. New outputs: `postgresServerName`, `postgresServerFqdn`, `postgresDatabaseName`, `evidenceStorageAccountName`, `evidenceBlobEndpoint`, `evidenceContainerName`, `auditAnchorContainerName`, `auditEpochContainerName`, `contractsContainerName`.
* The estate contracts file now comes from a **blob** (`contractsContainerName` / `contractsBlobName`, uploaded after the deployment, section 4). The file used to live on the Azure Files share at `contractsPath`, which is gone together with `ORQENTIS_CONTRACTS_PATH` (a parameter file that still sets `contractsPath` no longer compiles). A `rest` app stays not ready until the blob is uploaded. (`contractsContent`, a Container App secret design, was considered and never shipped.)
* `postgresEntraAdminObjectId` and `postgresEntraAdminName` are required. `postgresPoolMax` now defaults to `6` (minimum `2`, as in the app), because the connection budget assumes two revisions at `maxReplicas`.
* Outside `dev` the template requires private networking (`infrastructureSubnetId`, `postgresDelegatedSubnetId`, `postgresPrivateDnsZoneId`) and rejects `postgresAllowAzureServices`. `externalAuthenticated` is now valid together with `infrastructureSubnetId` (a VNet-integrated environment with a public, authenticated endpoint). A new `audit-epochs` container and the optional `auditOperatorsPrincipalId` exist. The deploy identity's role-assignment condition must allow assigning to a `Group` as well as to service principals (docs/delivery.md).
* `schedulerStartupDelaySeconds` now defaults to `30` (it was `120`).
* `minReplicas` / `maxReplicas` default to `2` / `2` (the template used to be pinned to 1); `dev` sets 1.
* The deploy identity's role-assignment condition must now allow `Storage Blob Data Contributor` and `Storage Blob Data Reader`, and the `Microsoft.DBforPostgreSQL` provider must be registered ([docs/delivery.md](../../docs/delivery.md)).
* The evidence account is a **new** account (`stev...`). An environment that ran the earlier template keeps its old `st<prefix><hash>` account and file share, which the template no longer references and an incremental deployment does not delete. There is no importer for the old SQLite and JSONL data; if an environment holds data you need, export it from the share before deleting the account (`az storage account delete -g "$RG" -n <old account>`).

## Observability

* Application traces and logs go to Application Insights through `APPLICATIONINSIGHTS_CONNECTION_STRING`, which is a Container App secret and not a plain environment value. Local authentication stays enabled on the Application Insights component because the exporter authenticates with the connection string. Entra-only ingestion would need code support for a credential.
* Container stdout (JSON logs, `ORQENTIS_LOG_FORMAT=json`) and system logs go to the Log Analytics workspace (`ContainerAppConsoleLogs_CL`, `ContainerAppSystemLogs_CL`).
* The application's own metrics (collections, incidents, investigations, tools, model tokens, scheduler, audit verification; `src/orqentis/observability/metrics.py`) are exported with the traces and land in the workspace's `AppMetrics` table. Application logs deliberately reach the workspace through the console logs only, not a second time as `AppTraces` (that would double the ingestion bill).
* `logDailyQuotaGb` caps the workspace's daily ingestion (`-1`, the default, is no cap; `env/dev.bicepparam` sets `1`). A cap stops ingestion when it is reached, including the metrics the alerts read, so the heartbeat alert fires: use it for a sandbox, not for production.

## Monitoring and alerts

`monitoringEnabled` (default `true`) deploys `modules/monitoring.bicep`: an action group, six alert rules and a workbook. Everything in the module is derived from the ids `main.bicep` passes in (workspace, Container App), so `what-if` works anywhere. Per-alert meaning, the first things to check and how to test-fire each one are in [`docs/operations.md`](../../docs/operations.md); the design is in [ADR-0019](../../docs/adr/0019-metrics-alerts-and-operations.md).

| Alert rule | Severity | Fires when | Window / threshold parameters (defaults) |
|---|---|---|---|
| `<prefix>-app-down` (metric alert on `Replicas`) | 1 | the Container App had no running replica for the whole window | `alertAppDownWindowMinutes` (5) |
| `<prefix>-restart-loop` (log alert) | 1 | the process started, or its container was terminated, `alertRestartThreshold` times in the window | `alertRestartWindowMinutes` (15), `alertRestartThreshold` (3) |
| `<prefix>-5xx-rate` (log alert) | 2 | at least `alertHttp5xxRatePercent` % of the requests in the window answered 5xx (windows with fewer than `alertHttp5xxMinRequests` requests are ignored; health probes are excluded) | `alertHttp5xxWindowMinutes` (10), `alertHttp5xxRatePercent` (5), `alertHttp5xxMinRequests` (20) |
| `<prefix>-collector-errors` (log alert, one alert per collector) | 2 | one collector had `alertCollectorErrorRuns` failing runs in the window | `alertCollectorErrorWindowMinutes` (15), `alertCollectorErrorRuns` (2) |
| `<prefix>-collection-heartbeat` (log alert) | 1 | no successful collection (a collect + detect run in which not everything failed) for the window; switched off while `schedulerEnabled` or `collectEnabled` is `false` | `alertHeartbeatMinutes` (15) |
| `<prefix>-audit-verification-failure` (log alert) | 0 | a scheduled audit verification failed or could not read the log, or an append was refused after a shrink. It does not auto-resolve (a person closes it) and re-notifies at most every 6 hours | `alertAuditFailureWindowMinutes` (0: derived as twice `auditVerifyIntervalMinutes`, rounded up to an allowed window: 120 by default) |

`alertEvaluationFrequencyMinutes` (default `5`; 1, 5, 10, 15, 30, 45 or 60) sets how often the log alerts are evaluated; the metric alert is evaluated every minute. Azure accepts only certain windows for a log alert, so each window parameter is limited to 5, 10, 15, 30, 45, 60, 120, 180, 240, 300, 360, 1440 or 2880 minutes (the app-down window to 1, 5, 15, 30 or 60), and no window may be shorter than the evaluation frequency: the deployment stops with a message naming the windows that are too short. Each rule description and each notification carries the runbook link `<alertRunbookBaseUrl>#alert-...` (`alertRunbookBaseUrl` defaults to `docs/operations.md` on `main` of the public repository; point it at your fork or an internal copy).

**Who is told, preferred: `alertExistingActionGroupId`.** The resource id of an action group that is created and managed outside the template (`az monitor action-group create`). When it is set, every alert uses it and the template **creates and changes no action group**; people are added with `az monitor action-group update` or the portal. It is an identifier, so the pipeline can carry it (`env/*.bicepparam` read `ALERT_ACTION_GROUP_ID`, a GitHub variable): this repository is public and a GitHub Actions job prints its environment in its log, so addresses must never go through CI. A malformed id, or an id together with the two receiver parameters below, stops the deployment with a message that says so (the receivers would otherwise be ignored without a word). The deploying identity needs read access to the group. Deployment outputs: `alertActionGroupId`.

**Who is told, otherwise (private repositories and local shells only): the template's own group.** `alertEmailReceivers` (an array of addresses) and `alertTeamsWebhookUri` (`@secure()`, a Microsoft Teams Workflows webhook URL) are the receivers of the group the template creates. The parameter files never contain them: `env/*.bicepparam` read the environment variables `ALERT_EMAIL_RECEIVERS` (comma separated) and `ALERT_TEAMS_WEBHOOK_URI` of whoever deploys, and both default to empty. With both empty the rules still evaluate and show in the portal, but nobody is notified; the deployment outputs `alertEmailReceiverCount` so a pipeline or a person can check. The CD workflow never sets these two (it only carries `ALERT_ACTION_GROUP_ID`), and a deployment that omits a receiver removes it, because the template owns this group: set the variables wherever the deployment runs (`docs/operations.md`, *Alert receivers*). The webhook URL carries its own signature: keep it in the deployer's secret store, not in a parameter file, a repository or a CI variable. Action group receivers are readable by anyone who can read the action group, so keep Reader on the resource group to the operators.

**Prerequisites and limits.** The log alerts read `AppMetrics`, `AppRequests` and the Container Apps log tables of the workspace; they exist only after the first rows have been ingested, which is why the rules are deployed with `skipQueryValidation`. The metrics reach `AppMetrics` only while `APPLICATIONINSIGHTS_CONNECTION_STRING` is set (the template always sets it). The app-down alert relies on the platform metric `Replicas`; it does not see a replica that runs but is not ready (that shows as a restart loop, a missing heartbeat or 5xx).

## Validate locally

```bash
az bicep build --file infra/bicep/main.bicep --stdout > /dev/null
az bicep lint --file infra/bicep/main.bicep
for f in infra/bicep/main.bicepparam infra/bicep/examples/*.bicepparam; do
  az bicep build-params --file "$f" --stdout > /dev/null
done
bash scripts/validate-bicep.sh      # main.bicep, main.bicepparam, examples and env/*.bicepparam, plus the prod write-switch check
```

`main.bicepparam` and the examples compile without any environment variable (they carry placeholders). The `env/*.bicepparam` files read the image, registry, API client id and PostgreSQL administrator from environment variables (`CONTAINER_IMAGE`, `ACR_LOGIN_SERVER`, `ACR_RESOURCE_ID`, `ENTRA_API_CLIENT_ID`, `POSTGRES_ENTRA_ADMIN_OBJECT_ID`, `POSTGRES_ENTRA_ADMIN_NAME`, and for `test` and `prod` `CONTAINER_APPS_SUBNET_ID`, `POSTGRES_DELEGATED_SUBNET_ID`, `POSTGRES_PRIVATE_DNS_ZONE_ID`, and for `prod` `EVIDENCE_IMMUTABILITY_LOCKED`) with no defaults, and optionally `EVIDENCE_IMMUTABILITY_LOCKED` (`dev`, `test`; default `false`), `AUDIT_OPERATORS_OBJECT_ID`, `FABRIC_MODE`, `POSTGRES_ENTRA_ADMIN_TYPE` and `POSTGRES_EXISTING_SERVER_NAME`; `scripts/validate-bicep.sh` supplies placeholders. CI (`.github/workflows/ci.yml`) runs the build steps and `scripts/validate-bicep.sh` on every push and pull request, and `.github/workflows/bicep-diff.yml` posts an offline diff of what each environment would deploy (`bicep snapshot`, no Azure access) on pull requests. The deploy job records the real `az deployment group what-if` with the environment's own deploy identity just before it deploys.
