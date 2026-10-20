# Orqentis FabricOps: Azure deployment (Bicep)

`main.bicep` deploys FabricOps into **one resource group** on Azure Container Apps. The app is stateless: its system of record is an Azure Database for PostgreSQL Flexible Server, and its evidence is in Blob storage with version-level immutability ([ADR-0008](../../docs/adr/0008-production-storage.md); implementation choices in ADR-0017). The template contains no credentials. Identities are managed, PostgreSQL accepts Microsoft Entra tokens only (password authentication is disabled), storage Shared Key access is off, and the Application Insights connection string is stored as a Container App secret.

| Resource | Name | Purpose |
|---|---|---|
| Log Analytics workspace | `log-<prefix>` | Container Apps console/system logs; backing store for Application Insights |
| Application Insights (workspace-based) | `appi-<prefix>` | OpenTelemetry traces, logs and metrics from the app (`azure-monitor` extra) |
| User-assigned managed identity | `id-<prefix>-reader` | **READ** capability boundary. The app's operating identity (Fabric/Power BI read, Azure OpenAI) |
| User-assigned managed identity | `id-<prefix>-actor` | **WRITE** capability boundary. Created, but attached to the app **only** when `enableWriteActions = true` |
| PostgreSQL Flexible Server | `psql-<prefix>-<hash>` (`modules/postgres.bicep`) | The system of record: incidents, approvals, events, scheduler leases, investigation jobs, the audit trail. Microsoft Entra authentication only (`passwordAuth` disabled), an Entra administrator (`postgresEntraAdminObjectId`), database `orqentis`, point-in-time restore. See *Storage* |
| Storage account | `stev<prefix><hash>` (`modules/evidence-storage.bicep`) | Evidence, reports and audit anchors in Blob containers `evidence` and `audit-anchors`. Entra only (Shared Key off), versioning, soft delete, version-level immutability |
| Role assignments | `modules/evidence-storage.bicep` | `Storage Blob Data Contributor` for the **reader** identity on each of the two containers (not on the account). The actor identity gets nothing on storage |
| Container Apps environment | `cae-<prefix>` | Workload-profiles environment (Consumption), logs to Log Analytics, optional VNet |
| Container App | `ca-<prefix>` | The API (`orqentis serve`), port 8000, `minReplicas` to `maxReplicas` replicas (2 by default; dev uses 1), Entra ID authentication, exposure per `ingressMode` |
| Container App auth config | `current` | Built-in authentication (Easy Auth) with Microsoft Entra ID, `Return401`; `/health` and `/health/ready` excluded by default (`anonymousHealthEndpoints`) |
| Role assignment (optional) | n/a | `AcrPull` for the reader identity on an ACR in the same RG (`containerRegistryName`) |
| Role assignment (optional) | `modules/acr-pull.bicep` | `AcrPull` for the reader identity on an ACR in any resource group (`containerRegistryResourceId`), such as the shared registry used by every environment |
| Role assignment (optional) | `modules/openai-role.bicep` | `Cognitive Services OpenAI User` for the reader identity on `azureOpenAiResourceId` (any RG/subscription) |
| Role assignment (optional) | `modules/keyvault-role.bicep` | `Key Vault Secrets User` for the reader identity on **each referenced secret only** (not the vault), in the vault of `keyVaultResourceId` (any RG/subscription) |
| Deployment check | `modules/input-check.bicep` | Fails the deployment with a readable reason when the `service_principal` inputs cannot work (see *Cross-tenant Fabric access*), and when the PostgreSQL administrator, the estate contracts (`rest` mode), the replica counts, the connection budget or the network options cannot work (see *Storage*) |
| Container App secret | `estate-contracts` | The estate contracts file (`contractsContent`), mounted read-only at `/mnt/contracts/estate.json`; only when `contractsContent` is set (see *Estate contracts*) |
| Container App secrets (optional) | `fabric-sp-client-secret` / `fabric-sp-certificate` | Key Vault references (`keyVaultUrl` + the reader identity), only in `service_principal` mode |

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
| `ORQENTIS_CONTRACTS_PATH` | `contractsPath` (default `/mnt/contracts/estate.json`, where `contractsContent` is mounted read-only) |
| `ORQENTIS_ENABLE_WRITE_ACTIONS` | `enableWriteActions` (default `false`) |
| `ORQENTIS_REASONER` | `reasoner` (default `rule_based`) |
| `ORQENTIS_AZURE_OPENAI_ENDPOINT` / `_DEPLOYMENT` | `azureOpenAiEndpoint` / `azureOpenAiDeployment` (only set when non-empty) |
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
| `ORQENTIS_INVESTIGATION_MAX_CONCURRENCY` / `_TIMEOUT_SECONDS` | `investigationMaxConcurrency` / `investigationTimeoutSeconds` (default `2` / `600`) |
| `ORQENTIS_INVESTIGATION_POLL_INTERVAL_SECONDS` / `ORQENTIS_INVESTIGATION_CLAIM_TTL_SECONDS` | `investigationPollIntervalSeconds` / `investigationClaimTtlSeconds` (default `2` / `120`; the TTL must be at least three times the poll interval). Every replica polls for queued durable investigation jobs; a claim that is not renewed within the TTL is released to another replica |
| `ORQENTIS_SCHEDULER_AUDIT_ANCHOR_ENABLED` / `ORQENTIS_SCHEDULER_AUDIT_ANCHOR_INTERVAL_MINUTES` | `auditAnchorEnabled` / `auditAnchorIntervalMinutes` (default `true` / `15`): the audit chain head is written to an immutable blob on this schedule |
| `ORQENTIS_STORAGE_BACKEND` | `postgres`, always. `local` (SQLite and files) is for development and tests only, and the app refuses it outside `local` |
| `ORQENTIS_POSTGRES_HOST` / `ORQENTIS_POSTGRES_DATABASE` | the server's FQDN (output `postgresServerFqdn`) / `postgresDatabaseName` (default `orqentis`) |
| `ORQENTIS_POSTGRES_USER` | the reader identity's **name**, `id-<prefix>-reader`: the PostgreSQL role the bootstrap step creates for the managed identity. The identity's Entra token is the password; there is no other credential |
| `ORQENTIS_POSTGRES_POOL_MAX` | `postgresPoolMax` (default `8`): connections per replica; see the connection budget under *Storage* |
| `ORQENTIS_EVIDENCE_BLOB_URL` / `ORQENTIS_EVIDENCE_CONTAINER` / `ORQENTIS_AUDIT_ANCHOR_CONTAINER` | the evidence account's blob endpoint (output `evidenceBlobEndpoint`) / `evidenceContainerName` (default `evidence`) / `auditAnchorContainerName` (default `audit-anchors`) |
| `ORQENTIS_FABRIC_MAX_CONCURRENCY` / `_RATE_PER_SECOND` / `_RATE_BURST` | `fabricMaxConcurrency` / `fabricRatePerSecond` / `fabricRateBurst` (default `4` / `5` / `10`) |
| `ORQENTIS_FABRIC_JOB_INSTANCES_MAX_PAGES` | `fabricJobInstancesMaxPages` (default `10`) |
| `ORQENTIS_STARTUP_GRACE_SECONDS` / `ORQENTIS_BUILD_RETRY_INITIAL_SECONDS` / `_MAX_SECONDS` | `startupGraceSeconds` / `buildRetryInitialSeconds` / `buildRetryMaxSeconds` (default `10` / `2` / `60`) |
| `ORQENTIS_COLLECTION_MAX_LOOKBACK_HOURS` / `_OVERLAP_MINUTES` | `collectionMaxLookbackHours` / `collectionOverlapMinutes` (default `168` / `15`) |
| `ORQENTIS_API_MAX_BODY_BYTES` | `apiMaxBodyBytes` (default `65536`): larger request bodies get 413 |
| `ORQENTIS_API_RATE_LIMIT_MUTATING_PER_MINUTE` | `apiRateLimitMutatingPerMinute` (default `30`), per principal and replica |
| `ORQENTIS_API_RATE_LIMIT_EXPENSIVE_PER_MINUTE` | `apiRateLimitExpensivePerMinute` (default `6`): collection, investigations, audit verification |
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

* **One audit chain.** An append locks the single chain-head row (`SELECT ... FOR UPDATE`), reads the last sequence and hash, inserts the record and moves the head in one transaction, so appends from any number of replicas, old revision and new revision included, form one linear chain. `GET /audit/verify` / `orqentis audit` verify it, and check it against the latest anchor.
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

Container Apps authentication validates bearer tokens issued for this app registration. It never runs an interactive login in this configuration, so no client secret is needed. This registration is documentation for a person with the right Entra permissions to run once. The repository's automation never runs it.

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

Copy `main.bicepparam` and set `entraClientId` to `$API_APP_ID`, along with `containerImage`, `containerRegistryName` and the other values you need. Two more values are **required**: the PostgreSQL Entra administrator (`postgresEntraAdminObjectId` and `postgresEntraAdminName`: the object id and name of an Entra group whose members will run the bootstrap step below; the deployment fails with a readable reason when they are empty) and, in `rest` mode, `contractsContent` (the estate contracts file, step 4). The subscription needs the `Microsoft.DBforPostgreSQL` resource provider registered once (`az provider register --namespace Microsoft.DBforPostgreSQL`).

```bash
az deployment group what-if -g "$RG" -f infra/bicep/main.bicep -p infra/bicep/main.bicepparam
az deployment group create  -g "$RG" -f infra/bicep/main.bicep -p infra/bicep/main.bicepparam -n fabricops
az deployment group show -g "$RG" -n fabricops --query properties.outputs
```

When `containerRegistryName` names an ACR in the same resource group, the template grants the reader identity `AcrPull` and pulls with it. For a registry in another resource group (the shared registry the pipeline uses), set `containerRegistryServer` to its login server and `containerRegistryResourceId` to its resource id: the template then grants the reader identity `AcrPull` on it through `modules/acr-pull.bicep` before it creates the app, so the deploying principal needs a role assignment right on that registry and the right to create deployments in the registry's resource group, because the module is a nested deployment there (docs/delivery.md, section 5). If you set only `containerRegistryServer`, grant `AcrPull` to the `readerIdentityPrincipalId` output yourself; the first revision fails to pull until that grant exists. After granting, restart the revision.

### 3a. PostgreSQL bootstrap (once per environment)

The template creates the server and its Entra administrator, but a template cannot create database roles. Until the app's managed identity exists **inside** PostgreSQL the app cannot connect, and `/health/ready` answers `503` (`/health` stays `200`). A member of the Entra administrator group (`postgresEntraAdminName`) creates the role once per environment:

* `az login` as that person, `psql` 14 or later, and network access to the server. With public access the server's firewall must allow your address: add it to `postgresAllowedIpRanges` and redeploy, or create a temporary rule (`az postgres flexible-server firewall-rule create -g "$RG" -s "$SERVER" -n bootstrap-me --start-ip-address <your address> --end-ip-address <your address>`) and delete it afterwards (`firewall-rule delete`). With VNet integration, run it from a host inside the network.
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
psql "host=$FQDN port=5432 dbname=postgres user=$ADMIN sslmode=require" -v ON_ERROR_STOP=1 \
  -f scripts/postgres-bootstrap.sql \
  -v app_role="$READER_ROLE" -v app_oid="$READER_OID" -v app_db="$DB" -v app_schema=orqentis
unset PGPASSWORD
```

`scripts/postgres-bootstrap.sql` creates the Entra principal for the managed identity with `pgaadauth_create_principal_with_oid` (so that identity's token maps to the role `id-<prefix>-reader`), the schema `orqentis` owned by that role, and grants it `CONNECT` on the database. It contains no secret. The token lasts about an hour; if `psql` reports an expired token, run `az account get-access-token` again.

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

If `/health/ready` stays `503`, read the replica log (`az containerapp logs show -g "$RG" -n "$APP" --follow`): `role "id-..." does not exist` means the bootstrap did not run against this server and database; a connection timeout means the firewall or the network (public access needs `postgresAllowAzureServices` or a rule for the environment's outbound addresses, VNet integration needs the environments in one network); an Entra token error points at `ORQENTIS_MANAGED_IDENTITY_CLIENT_ID` or at the role being created for another identity.


### 4. Estate contracts (rest mode)

In `rest` mode the app needs the declared environments, topology, data products and SLAs at `contractsPath`. There is no shared disk to upload to any more: the file is deployed with the app. The parameter `contractsContent` takes the **JSON text of the file**; the template stores it as the Container App secret `estate-contracts` and mounts it read-only as `/mnt/contracts/estate.json` (the same pattern as the Key Vault certificate in *Cross-tenant Fabric access*). In `rest` mode the deployment **fails with a readable reason** when `contractsContent` is empty; in `mock` mode it may be empty and nothing is mounted.

If the file is invalid the app does not start; the container log carries one error that names the path and lists every violation as `file:line:column: error: /json/pointer: message`, for example `Contracts file is invalid: /mnt/contracts/estate.json` followed by `... error: /slas/0/timezone: not a valid IANA time zone`. Container Apps keeps restarting the container until the file is fixed, so check the file **before** deploying it:

```bash
orqentis contracts scaffold --output contracts/estate.json --catalog-output contracts/catalog.json   # draft from a read-only listing
# ... review and edit the draft (see docs/development.md, "Contracts workflow") ...
orqentis contracts validate contracts/estate.json --catalog contracts/catalog.json --strict
```

The file format is described by `schemas/contracts.schema.json`. A full estate file (`fixtures/fabric/estate.json`) is also accepted; only its contract sections are used.

Hand the text to the deployment:

```bash
# Pipeline: the text of the file is the GitHub environment variable CONTRACTS_JSON; the env/*.bicepparam files read it with
# readEnvironmentVariable('CONTRACTS_JSON', '') (docs/delivery.md, section 7).
gh variable set CONTRACTS_JSON --repo mjtpena/fabricops-digital-agent --env prod < contracts/estate.json

# Manual deployment from a shell:
export CONTRACTS_JSON="$(cat contracts/estate.json)"
az deployment group create -g "$RG" -f infra/bicep/main.bicep -p infra/bicep/main.bicepparam -n fabricops
# or, in your own parameter file:  param contractsContent = loadTextContent('../../contracts/estate.json')
```

The file holds identifiers, names and SLAs, not credentials. It is a secure parameter and a Container App secret only so that it stays out of deployment logs; a GitHub variable is readable by everyone with access to the repository, like the other variables. To change the contracts, update the variable and deploy again, then restart the active revision so the app reads the new file (a secret change does not create a revision; `az containerapp revision restart`).

**Size.** Container Apps limits the size of a secret, and GitHub limits a variable to 48 KB. I could not verify the Container Apps limit offline: verify it for your file (`wc -c contracts/estate.json`). For a very large estate file, use a Key Vault secret reference instead of `contractsContent`: the template already resolves Key Vault references with the reader identity for the cross-tenant credential, and a contracts secret has the same shape; that variant is not in the template yet.

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
3. **Power BI semantic model refresh history is not readable with Viewer (observed).** The reader identity with workspace `Viewer` gets **403 `Unauthorized`** on `GET /v1.0/myorg/groups/{groupId}/datasets/{datasetId}/refreshes` (sandbox model, 02:22:51Z to 02:28:18Z on 2026-10-20; `tests/live/test_live_rest.py::test_refresh_history_needs_more_than_viewer`). Its effective dataset right is already `Read` (`GET .../datasets/{id}/users` lists it), so Viewer's read of the model's metadata works, but not this call. What was tried to fix it, and failed: dataset `Read` and `ReadExplore` cannot be assigned to a service principal through the REST API (400 "API supported only for User or Group principal types"), and the Fabric job-instances API answers 404 for a semantic model, even to the owner. Microsoft documents the requirement as write permission on the model ("Caller must have Write permissions on the dataset"), which is what the 403 is consistent with; the programme did not try Contributor because it is a write role, and **no write was granted**. The one read-only route that works is `GET .../datasets/{datasetId}/refreshes/{requestId}` (Get Refresh Execution Details: 200 with Viewer for completed and failed refreshes, with the failure messages), which needs the refresh's request id and cannot list them. Evidence and timestamps: `docs/security.md` section 11. With Viewer only, `POST /telemetry/collect` reports `collector_errors["semantic-model-refreshes"]` for each monitored model and every other collector carries on. Accept that gap, or decide explicitly to raise the privilege or to source refresh outcomes elsewhere.
4. **Capacities and deployment pipelines.** Do not give the reader identity capacity roles or deployment-pipeline Admin. Both carry write permissions: assigning workspaces to a capacity, or deploying. Without them, capacity listing and deployment-state lookups return nothing for that identity. CU utilisation is not available from any public API anyway; ingest Capacity Metrics into Eventhouse instead.
5. **OneLake `_delta_log` reads (schema-drift collector, ADR-0009).** Workspace `Viewer` gives no OneLake access (every path is 403). The smallest working grant is a *OneLake data access role* with `Read` on one table, created per source lakehouse with `PUT https://api.fabric.microsoft.com/v1/workspaces/{workspaceId}/items/{lakehouseId}/dataAccessRoles` (the call replaces the whole role set, so include the built-in `DefaultReader` role unchanged). Roles exist only at lakehouse-root and table level, so the log cannot be granted without the table's data. It is read-only: other tables, `Files` and every write stay 403. The role body, the proof and the limits are in `docs/security.md` section 11; `scripts/live-estate/estate.sh` (`ensure_table_reader_role`) is a working example.
6. **Actor identity: `Contributor` only on workspaces where writes are permitted.** Do this only once a write executor is in use and `enableWriteActions = true`. Use the same portal flow or REST call with `"role": "Contributor"`. Never grant `Admin` or `Member`. Never grant it on workspaces outside the write scope.

**Verify.** As an operator, call `POST /telemetry/collect` and check that `collector_errors` is empty. A 401/403 entry names the API and workspace that still needs a grant, except `semantic-model-refreshes`, which stays 403 for a Viewer-only identity (item 3 above).

> In this MVP the REST adapter is **read-only**. With `fabricMode = 'rest'`, approved actions are executed by `DisabledFabricWriteClient`, which refuses them even when `enableWriteActions = true`. The actor identity and the switch exist so that a future write executor can be enabled without touching the read path. Keeping the two identities separate means a compromised read path holds no write token. The strongest form of that separation is to run the write executor as its own app with only the actor identity attached.

### 6. Azure OpenAI (optional, `reasoner = 'agent_framework'`)

Set `azureOpenAiEndpoint`, `azureOpenAiDeployment` and `azureOpenAiResourceId`. The template grants the reader identity **Cognitive Services OpenAI User** on that account, which permits inference only (no keys, no deployments). The grant is made through `modules/openai-role.bicep`, scoped to the account's own resource group and subscription. The deploying principal needs permission to create role assignments there. Without endpoint and deployment the app refuses to start with `agent_framework`.

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
| `externalAuthenticated` (default) | must be empty | `true` | Anyone on the internet **who presents a valid token**. Add `allowedIpRanges` to limit by address before authentication. |
| `vnet` | required | `true` in an internal environment | Only the VNet (no public IP): peered networks, VPN, ExpressRoute. Front it with Application Gateway/APIM or create a private DNS zone for the environment's default domain. |
| `internal` | optional | `false` | Only other apps in the same Container Apps environment. People cannot reach it. |

The subnet must be delegated to `Microsoft.App/environments` and be at least `/27`, because the environment uses workload profiles. A mismatch (`vnet` without a subnet, or `externalAuthenticated` with one) feeds an invalid subnet id into the environment, so the deployment **fails** instead of silently exposing the API differently from what was asked. Ready-made parameter files are in [`examples/`](examples/), one per mode (plus the deprecated `externalIngress` override). Build them with the loop under *Validate locally*; CI currently builds `main.bicepparam` only.

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

## Storage

A deployed environment keeps **no state in the container**. Two stateful services hold it, both reached with the app's managed identity and no secret:

| What | Where | Why |
|---|---|---|
| Incidents, approvals, dependencies, operational events with collector watermarks, scheduler leases, investigation jobs, the audit trail (records and chain head) | PostgreSQL Flexible Server `psql-<prefix>-<hash>`, database `orqentis` (`modules/postgres.bicep`) | Transactions, row locks and compare-and-set make concurrent replicas safe; backups with point-in-time restore |
| Evidence artefacts and incident reports | Blob container `evidence` (`ORQENTIS_EVIDENCE_CONTAINER`) | Write-once; version-level immutability |
| Audit-chain anchors | Blob container `audit-anchors` (`ORQENTIS_AUDIT_ANCHOR_CONTAINER`) | The chain head (sequence and hash), copied periodically to storage the database cannot rewrite |
| The estate contracts file | Container App secret `estate-contracts`, a read-only file at `/mnt/contracts/estate.json` | Its source of truth is the repository / the `CONTRACTS_JSON` variable, not this deployment |
| `ORQENTIS_DATA_DIR` (`/var/lib/orqentis`) | The container's own disk | Ephemeral and per replica: scratch space, lost when the replica restarts. Nothing durable is written there |

There is no Azure Files share, no SQLite file and no storage account key in a deployed environment. `ORQENTIS_STORAGE_BACKEND=postgres` is set by the template, and the app refuses to start on local files in `dev`, `test` or `prod`. SQLite, the file evidence store and the JSONL audit log remain the adapters for local development, tests and `make demo` ([ADR-0003](../../docs/adr/0003-local-stores-first.md)).

### PostgreSQL

* **Entra only.** `authConfig` has `activeDirectoryAuth: Enabled` and `passwordAuth: Disabled`: the server has no administrator login and no password. A connection presents an Entra access token for `https://ossrdbms-aad.database.windows.net` as the password. The app uses the reader managed identity (the database role is named after it, `id-<prefix>-reader`); people use their own token. TLS is required (`require_secure_transport` is `ON`).
* **Administrator.** `postgresEntraAdminObjectId` and `postgresEntraAdminName` are **required**; `postgresEntraAdminType` is `Group` (default), `User` or `ServicePrincipal`. Use a group, so people can be added and removed without touching the server. Its members run the bootstrap step (section 3a). The pipeline reads the values from the `POSTGRES_ENTRA_ADMIN_OBJECT_ID`, `POSTGRES_ENTRA_ADMIN_NAME` and `POSTGRES_ENTRA_ADMIN_TYPE` variables ([docs/delivery.md](../../docs/delivery.md)). Setting the administrator needs no Azure role beyond the deploy identity's `Contributor` on the resource group.
* **Sizing.** `postgresSkuName` (default `Standard_B1ms`, Burstable; `prod` uses `Standard_D2ds_v5`; the tier is derived from the name), `postgresStorageSizeGiB` (default `32`; storage auto-grows, and once it has grown, raise the parameter to the current size before the next deployment because a smaller value is refused), `postgresVersion` (default `17`; applied when the server is created), `postgresBackupRetentionDays` (default `7`, up to `35`), `postgresGeoRedundantBackup` (default `false`; fixed when the server is created, it copies backups to the paired region, which is a data-residency decision), `postgresHighAvailabilityMode` (`Disabled`, `ZoneRedundant` or `SameZone`; not on the Burstable tier, and the deployment fails if you combine them) and `postgresDatabaseName` (default `orqentis`).
* **Connection budget.** Every replica opens up to `postgresPoolMax` connections (`ORQENTIS_POSTGRES_POOL_MAX`, default `8`). During a deploy the old revision (up to `maxReplicas`) and the new one (`minReplicas`) run together, so the demand is `(maxReplicas + minReplicas) x postgresPoolMax`, and about 15 connections are reserved for the platform. `Standard_B1ms` allows about 50 (`max_connections`): `test`'s 2 + 2 replicas x 8 = 32, plus 15 = 47, fits, and the deployment **fails** on `Standard_B1ms` when the plan needs more than 50. Other SKUs are not checked by the template; read the limit with `SHOW max_connections;` (it grows with the SKU) and leave room for people and jobs that connect with `psql`.
* **Network, option 1: public access with a firewall** (default). `postgresAllowedIpRanges` is an array of `{ name, startIpAddress, endIpAddress }` rules; it is empty by default, so nobody outside Azure can connect: add the address of whoever runs the bootstrap step. `postgresAllowAzureServices` (template default `false`; `dev`, `test` and `prod` set `true`) adds the documented `0.0.0.0` rule that lets Azure services connect, which is what lets Container Apps reach the server, because an environment without a VNet has no fixed outbound address. It only opens the TCP port to resources inside Azure: **every connection still needs a valid Entra token for an existing database role.** Prefer option 2 for production once a network exists.
* **Network, option 2: VNet integration (private).** `postgresDelegatedSubnetId` (a subnet delegated to `Microsoft.DBforPostgreSQL/flexibleServers`, not the Container Apps subnet) together with `postgresPrivateDnsZoneId`. The server then has no public endpoint. The Container Apps environment must be in the same or a peered VNet (`ingressMode: 'vnet'`, `infrastructureSubnetId`), and the bootstrap step must run from inside the network. Firewall rules cannot be combined with it: the deployment fails if you set both. See [`examples/ingress-vnet.bicepparam`](examples/ingress-vnet.bicepparam).
* **Use an existing server.** `postgresExistingServerName` is empty by default. Set it to the name of a server in the same resource group, for example one restored from a backup, and the template creates nothing: it points the app at that server and leaves it alone (its Entra administrator, firewall rules and parameters are then yours to keep). [docs/operations/restore.md](../../docs/operations/restore.md) uses it.
* Encryption at rest uses the platform's service-managed keys; customer-managed keys are not configured.

### Evidence and audit anchors (Blob)

`modules/evidence-storage.bicep` creates a StorageV2 account (`stev<prefix><hash>`) with `allowSharedKeyAccess: false` (no account key is in use: not for the app, the pipeline or the portal; use `--auth-mode login`), Entra as the default authentication, TLS 1.2, HTTPS only, no public blob access, no cross-tenant replication and **no hierarchical namespace**: it speaks the plain Blob API, which is also what the Azurite emulator speaks. The same adapter can write to a OneLake item instead of this account by setting `ORQENTIS_EVIDENCE_BLOB_URL` (the OneLake blob endpoint), `ORQENTIS_EVIDENCE_CONTAINER` (the workspace) and `ORQENTIS_EVIDENCE_PATH_PREFIX` (the item folder); the template deploys the account either way.

* **Immutability.** Version-level immutability is enabled for the account with a default retention policy of `evidenceRetentionDays` (default `30`, up to `3650`) that every new blob version inherits: a version cannot be changed or deleted until its retention ends. The setting can only be switched on when an account is created, which is why this account has its own name (`stev...`, not the `st...` account of earlier template versions). An overwrite creates a new version and the previous one stays protected. *Verify on the first deployment:* reports that are re-rendered are written as overwrites; if the service refuses an overwrite of a blob whose current version is under retention, reports must be written under a new name per render instead.
* **`Unlocked` or `Locked`: the owner's decision.** `evidenceImmutabilityState` is `Unlocked` by default and in every environment today. Unlocked means an account owner (management-plane rights, which the app does not have) can still change or remove the policy: right for `dev` and `test`, and for `prod` until the retention period and the decision are confirmed. **`Locked` is irreversible:** the retention can then only be extended, never shortened or removed, and the account cannot be deleted until every blob's retention has expired. It is a deliberate production step that needs the owner's approval. To lock: set `evidenceImmutabilityState = 'Locked'` in `env/prod.bicepparam` in a reviewed pull request, after the account exists with the policy `Unlocked` (a policy cannot be created `Locked`; the platform allows only the Unlocked to Locked transition). `tests/unit/test_infra_consistency.py` guards the prod value, so the same pull request changes that test.
* **Soft delete and versioning.** Versioning is on. Blob soft delete and container soft delete keep a deleted or overwritten blob, and a deleted container, recoverable for `evidenceSoftDeleteDays` (default `14`). Change feed is off. Recover with `az storage blob undelete` and `az storage container restore`, and list versions with `az storage blob list --include v`.
* **Who can touch it.** The reader identity holds `Storage Blob Data Contributor` on each of the two containers (`evidenceContainerName`, default `evidence`, and `auditAnchorContainerName`, default `audit-anchors`), **not on the account**: it can read and write blobs in those two containers and nothing else. It cannot list or create containers (the template creates them), change policies or read account settings. The actor (WRITE) identity has no access to storage. There are no keys and no SAS. People who need to read evidence or anchors need a data role of their own, for example `Storage Blob Data Reader` on a container.
* **Network.** `evidenceNetworkDefaultAction` is `Allow` by default: the public endpoint answers any caller, and every request still needs an Entra token and a role. `Deny` with `evidenceAllowedIpRanges` and/or `evidenceAllowedSubnetIds` restricts it (trusted Azure services stay allowed). An environment without a VNet has no fixed address, so `Deny` is for ingress mode `vnet` with a subnet that has the `Microsoft.Storage` service endpoint; the deployment fails if `Deny` has neither a range nor a subnet.
* **Redundancy.** `storageSkuName`: `Standard_LRS` (`dev`, `test`) or `Standard_ZRS` (`prod`).

### Backups and restore

PostgreSQL backs itself up continuously; `postgresBackupRetentionDays` (7 to 35) is how far back a point-in-time restore can go. A restore creates a **new** server. [docs/operations/restore.md](../../docs/operations/restore.md) is the runbook, with the exact commands, what happens to evidence and the audit anchor, and the rehearsal checklist the owner signs off. Evidence and anchors are protected by versioning, soft delete and immutability, not by the database backup.

### Upgrading from the Azure Files template

This release changes the interface of the template:

* `fileShareQuotaGiB` is removed (a parameter file that still sets it no longer compiles), and so are the `storageAccountName` and `dataFileShareName` outputs. New outputs: `postgresServerName`, `postgresServerFqdn`, `postgresDatabaseName`, `evidenceStorageAccountName`, `evidenceBlobEndpoint`, `evidenceContainerName`, `auditAnchorContainerName`.
* `contractsContent` is required in `rest` mode, and `contractsPath` now defaults to `/mnt/contracts/estate.json`.
* `postgresEntraAdminObjectId` and `postgresEntraAdminName` are required.
* `schedulerStartupDelaySeconds` now defaults to `30` (it was `120`).
* `minReplicas` / `maxReplicas` default to `2` / `2` (the template used to be pinned to 1); `dev` sets 1.
* The deploy identity's role-assignment condition must now allow `Storage Blob Data Contributor`, and the `Microsoft.DBforPostgreSQL` provider must be registered ([docs/delivery.md](../../docs/delivery.md)).
* The evidence account is a **new** account (`stev...`). An environment that ran the earlier template keeps its old `st<prefix><hash>` account and file share, which the template no longer references and an incremental deployment does not delete. There is no importer for the old SQLite and JSONL data; if an environment holds data you need, export it from the share before deleting the account (`az storage account delete -g "$RG" -n <old account>`).

## Observability

* Application traces and logs go to Application Insights through `APPLICATIONINSIGHTS_CONNECTION_STRING`, which is a Container App secret and not a plain environment value. Local authentication stays enabled on the Application Insights component because the exporter authenticates with the connection string. Entra-only ingestion would need code support for a credential.
* Container stdout (JSON logs, `ORQENTIS_LOG_FORMAT=json`) and system logs go to the Log Analytics workspace (`ContainerAppConsoleLogs_CL`, `ContainerAppSystemLogs_CL`).

## Validate locally

```bash
az bicep build --file infra/bicep/main.bicep --stdout > /dev/null
az bicep lint --file infra/bicep/main.bicep
for f in infra/bicep/main.bicepparam infra/bicep/examples/*.bicepparam; do
  az bicep build-params --file "$f" --stdout > /dev/null
done
bash scripts/validate-bicep.sh      # main.bicep, main.bicepparam, examples and env/*.bicepparam, plus the prod write-switch check
```

`main.bicepparam` and the examples compile without any environment variable (they carry placeholders). The `env/*.bicepparam` files read the image, registry, API client id and PostgreSQL administrator from environment variables (`CONTAINER_IMAGE`, `ACR_LOGIN_SERVER`, `ACR_RESOURCE_ID`, `ENTRA_API_CLIENT_ID`, `POSTGRES_ENTRA_ADMIN_OBJECT_ID`, `POSTGRES_ENTRA_ADMIN_NAME`) with no defaults, and optionally `FABRIC_MODE`, `POSTGRES_ENTRA_ADMIN_TYPE`, `POSTGRES_EXISTING_SERVER_NAME` and `CONTRACTS_JSON`; `scripts/validate-bicep.sh` supplies placeholders. CI (`.github/workflows/ci.yml`) runs the build steps and `scripts/validate-bicep.sh` on every push and pull request, and `.github/workflows/whatif.yml` shows the what-if for `dev` on pull requests.
