# Orqentis FabricOps: Azure deployment (Bicep)

`main.bicep` deploys the FabricOps MVP into **one resource group** on Azure Container Apps. The template contains no credentials. Identities are managed, the Application Insights connection string is stored as a Container App secret, and the storage key is read at deployment time only to mount the Azure Files share.

| Resource | Name | Purpose |
|---|---|---|
| Log Analytics workspace | `log-<prefix>` | Container Apps console/system logs; backing store for Application Insights |
| Application Insights (workspace-based) | `appi-<prefix>` | OpenTelemetry traces, logs and metrics from the app (`azure-monitor` extra) |
| User-assigned managed identity | `id-<prefix>-reader` | **READ** capability boundary. The app's operating identity (Fabric/Power BI read, Azure OpenAI) |
| User-assigned managed identity | `id-<prefix>-actor` | **WRITE** capability boundary. Created, but attached to the app **only** when `enableWriteActions = true` |
| Storage account + Azure Files share | `st<prefix><hash>` / `orqentis-data` | Mounted at `/var/lib/orqentis`: MVP SQLite stores, evidence, audit log, contracts file |
| Container Apps environment | `cae-<prefix>` | Workload-profiles environment (Consumption), logs to Log Analytics, optional VNet |
| Container App | `ca-<prefix>` | The API (`orqentis serve`), port 8000, 1 replica, Entra ID authentication, exposure per `ingressMode` |
| Container App auth config | `current` | Built-in authentication (Easy Auth) with Microsoft Entra ID, `Return401`; `/health` and `/health/ready` excluded by default (`anonymousHealthEndpoints`) |
| Role assignment (optional) | n/a | `AcrPull` for the reader identity on an ACR in the same RG (`containerRegistryName`) |
| Role assignment (optional) | `modules/acr-pull.bicep` | `AcrPull` for the reader identity on an ACR in any resource group (`containerRegistryResourceId`), such as the shared registry used by every environment |
| Role assignment (optional) | `modules/openai-role.bicep` | `Cognitive Services OpenAI User` for the reader identity on `azureOpenAiResourceId` (any RG/subscription) |
| Role assignment (optional) | `modules/keyvault-role.bicep` | `Key Vault Secrets User` for the reader identity on **each referenced secret only** (not the vault), in the vault of `keyVaultResourceId` (any RG/subscription) |
| Deployment check | `modules/input-check.bicep` | Fails the deployment with a readable reason when the `service_principal` inputs cannot work (see *Cross-tenant Fabric access*) |
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
| `ORQENTIS_CONTRACTS_PATH` | `contractsPath` (default `/var/lib/orqentis/contracts/estate.json`) |
| `ORQENTIS_ENABLE_WRITE_ACTIONS` | `enableWriteActions` (default `false`) |
| `ORQENTIS_REASONER` | `reasoner` (default `rule_based`) |
| `ORQENTIS_AZURE_OPENAI_ENDPOINT` / `_DEPLOYMENT` | `azureOpenAiEndpoint` / `azureOpenAiDeployment` (only set when non-empty) |
| `ORQENTIS_DATA_DIR` | `/var/lib/orqentis` (Azure Files mount) |
| `ORQENTIS_LOG_FORMAT` | `json` |
| `ORQENTIS_SCHEDULER_ENABLED` | `schedulerEnabled` (default `true`) |
| `ORQENTIS_SCHEDULER_STARTUP_DELAY_SECONDS` | `schedulerStartupDelaySeconds` (default `120`) |
| `ORQENTIS_SCHEDULER_TICK_SECONDS` / `_LEASE_TTL_SECONDS` / `_JOB_TIMEOUT_MINUTES` | `schedulerTickSeconds` / `schedulerLeaseTtlSeconds` / `schedulerJobTimeoutMinutes` (default `5` / `60` / `30`; the TTL must be at least three times the tick) |
| `ORQENTIS_SCHEDULER_COLLECT_ENABLED` / `_INTERVAL_MINUTES` | `collectEnabled` / `collectIntervalMinutes` (default `true` / `5`) |
| `ORQENTIS_SCHEDULER_AUTO_INVESTIGATE_ENABLED` / `_INTERVAL_MINUTES` | `autoInvestigateEnabled` / `autoInvestigateIntervalMinutes` (default `true` / `1`) |
| `ORQENTIS_SCHEDULER_AUTO_INVESTIGATE_BATCH_SIZE` / `_MAX_ATTEMPTS` | `autoInvestigateBatchSize` / `autoInvestigateMaxAttempts` (default `5` / `3`) |
| `ORQENTIS_SCHEDULER_SLA_SWEEP_ENABLED` / `_INTERVAL_MINUTES` | `slaSweepEnabled` / `slaSweepIntervalMinutes` (default `true` / `15`) |
| `ORQENTIS_SCHEDULER_CATALOG_REFRESH_ENABLED` / `_INTERVAL_MINUTES` | `catalogRefreshEnabled` / `catalogRefreshIntervalMinutes` (default `true` / `60`) |
| `ORQENTIS_INVESTIGATION_MAX_CONCURRENCY` / `_TIMEOUT_SECONDS` | `investigationMaxConcurrency` / `investigationTimeoutSeconds` (default `2` / `600`) |
| `ORQENTIS_FABRIC_MAX_CONCURRENCY` / `_RATE_PER_SECOND` / `_RATE_BURST` | `fabricMaxConcurrency` / `fabricRatePerSecond` / `fabricRateBurst` (default `4` / `5` / `10`) |
| `ORQENTIS_FABRIC_JOB_INSTANCES_MAX_PAGES` | `fabricJobInstancesMaxPages` (default `10`) |
| `ORQENTIS_STARTUP_GRACE_SECONDS` / `ORQENTIS_BUILD_RETRY_INITIAL_SECONDS` / `_MAX_SECONDS` | `startupGraceSeconds` / `buildRetryInitialSeconds` / `buildRetryMaxSeconds` (default `10` / `2` / `60`) |
| `ORQENTIS_COLLECTION_MAX_LOOKBACK_HOURS` / `_OVERLAP_MINUTES` | `collectionMaxLookbackHours` / `collectionOverlapMinutes` (default `168` / `15`) |
| `ORQENTIS_API_MAX_BODY_BYTES` | `apiMaxBodyBytes` (default `65536`): larger request bodies get 413 |
| `ORQENTIS_API_RATE_LIMIT_MUTATING_PER_MINUTE` | `apiRateLimitMutatingPerMinute` (default `30`), per principal and replica |
| `ORQENTIS_API_RATE_LIMIT_EXPENSIVE_PER_MINUTE` | `apiRateLimitExpensivePerMinute` (default `6`): collection, investigations, audit verification |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | Container App **secret** reference |

Not set by the template, because the safe default already applies under `easy_auth`: `ORQENTIS_API_REQUIRE_READ_ROLE` (every call needs a FabricOps role; see section 1) and `ORQENTIS_API_RATE_LIMIT_ENABLED` (`true`).

### Health probes and autonomous operation

The Container App has three probes (see the `probes` block of `main.bicep`):

| Probe | Path | Meaning |
|---|---|---|
| Startup | `/health` | The process is listening. It does not wait for the estate to be read: that happens in the background after the port opens, so a large tenant cannot fail the startup probe. |
| Liveness | `/health` | The process is alive. It is dependency-free and answers `200` while the container is still being built and while a failed build is being retried, so a bad contracts file or a Fabric outage never causes a restart loop. |
| Readiness | `/health/ready` | `503` (a fixed reason code and the exception type, never its message) until the container is built and the first catalog load finished, `200` afterwards. A failing build is retried with exponential backoff (`ORQENTIS_BUILD_RETRY_*`). |

With `schedulerEnabled = true` (the default) the app runs a small in-process scheduler: collect + detect every `collectIntervalMinutes`, automatic investigation of new incidents (as background jobs, at most `investigationMaxConcurrency` at a time), an SLA sweep, and a catalog refresh that applies workspace / item / dependency changes without a restart. Only the holder of an in-process leader lease runs the jobs. That is correct for the single replica this template deploys; the lease moves to the database with the PostgreSQL system of record, which is what allows more than one replica. All Fabric and Power BI calls share one concurrency and rate budget (`fabricMaxConcurrency`, `fabricRatePerSecond`, `fabricRateBurst`), and a `429` / `Retry-After` pauses every caller. Defaults are deliberately conservative; the real Fabric throttling quotas are not published per endpoint, so tune them from the `fabric http request` log lines (`status_code: 429`).

### Deploying an update: two revisions share the volume

Container Apps in `Single` revision mode runs the **old and the new revision side by side** while an update rolls out. They mount the same Azure Files share, each with its own in-process scheduler lease, and the local stores (SQLite, the hash-chained audit file) are single-writer. The template and the app reduce the risk, they cannot remove it:

* `schedulerStartupDelaySeconds` (default `120`) makes a new process wait before it takes the lease or runs any job, so the old revision drains first. The revision being replaced stops its scheduler and releases the lease when it receives the shutdown signal.
* The audit log re-reads its chain head before appending whenever the file grew since it last wrote, so appends from both revisions chain instead of forking. This is best effort (SMB has no byte-range locks).
* **Real multi-writer safety needs the PostgreSQL system of record** (database-backed lease, database-serialised audit chain). Until it exists, deploy with `schedulerEnabled = false`, or accept the risk and verify the chain afterwards with `GET /audit/verify` / `orqentis audit`. Nothing is deployed before that work.

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

Copy `main.bicepparam` and set `entraClientId` to `$API_APP_ID`, along with `containerImage`, `containerRegistryName` and the other values you need.

```bash
az deployment group what-if -g "$RG" -f infra/bicep/main.bicep -p infra/bicep/main.bicepparam
az deployment group create  -g "$RG" -f infra/bicep/main.bicep -p infra/bicep/main.bicepparam -n fabricops
az deployment group show -g "$RG" -n fabricops --query properties.outputs
```

When `containerRegistryName` names an ACR in the same resource group, the template grants the reader identity `AcrPull` and pulls with it. For a registry in another resource group (the shared registry the pipeline uses), set `containerRegistryServer` to its login server and `containerRegistryResourceId` to its resource id: the template then grants the reader identity `AcrPull` on it through `modules/acr-pull.bicep` before it creates the app, so the deploying principal needs a role assignment right on that registry and the right to create deployments in the registry's resource group, because the module is a nested deployment there (docs/delivery.md, section 5). If you set only `containerRegistryServer`, grant `AcrPull` to the `readerIdentityPrincipalId` output yourself; the first revision fails to pull until that grant exists. After granting, restart the revision.

### 4. Upload the estate contracts (rest mode)

In `rest` mode the app needs the declared environments, topology, data products and SLAs at `contractsPath`. If that file is missing or invalid the app does not start; the container log carries one error that names the path and lists every violation as `file:line:column: error: /json/pointer: message`, for example `Contracts file is invalid: /var/lib/orqentis/contracts/estate.json` followed by `... error: /slas/0/timezone: not a valid IANA time zone`. Container Apps keeps restarting the container until the file is fixed, so check the file **before** uploading it:

```bash
orqentis contracts scaffold --output contracts/estate.json --catalog-output contracts/catalog.json   # draft from a read-only listing
# ... review and edit the draft (see docs/development.md, "Contracts workflow") ...
orqentis contracts validate contracts/estate.json --catalog contracts/catalog.json --strict
```

The file format is described by `schemas/contracts.schema.json`. A full estate file (`fixtures/fabric/estate.json`) is also accepted; only its contract sections are used.

```bash
SA=$(az deployment group show -g "$RG" -n fabricops --query properties.outputs.storageAccountName.value -o tsv)
# Entra auth for Azure Files needs "Storage File Data Privileged Contributor" on the account for the uploader.
az storage directory create --account-name "$SA" --share-name orqentis-data --name contracts \
  --auth-mode login --enable-file-backup-request-intent
az storage file upload --account-name "$SA" --share-name orqentis-data \
  --source ./contracts/estate.json --path contracts/estate.json \
  --auth-mode login --enable-file-backup-request-intent
```

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
5. **What the app receives.** `ORQENTIS_AUTH_MODE=service_principal`, `ORQENTIS_TENANT_ID` and `AZURE_TENANT_ID` (Fabric tenant), `AZURE_CLIENT_ID` (service principal), and either `AZURE_CLIENT_SECRET` (a secret reference) or `AZURE_CLIENT_CERTIFICATE_PATH` (a read-only file mounted from the secret at `/mnt/fabric-sp/service-principal.pem`; the private key is never written to the Azure Files share). **Azure OpenAI keeps using the reader managed identity** (`ORQENTIS_LLM_AUTH_MODE=managed_identity`), because the model lives in the Azure tenant, not the Fabric one. If the credential is missing, or both a secret and a certificate are set, the app refuses to start and names the missing variables (never their values).
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

1. **First deployment with `ingressMode = 'internal'`** (nothing outside the environment can connect), `fabricMode = 'mock'` if you have not uploaded the contracts file yet.
2. **Verify authentication is on:** `az containerapp auth show -g "$RG" -n ca-<prefix>` must show `platform.enabled: true` and `globalValidation.unauthenticatedClientAction: Return401`.
3. **Then switch** to `externalAuthenticated` (or `vnet`) and redeploy. Check from outside: `curl -i https://<fqdn>/incidents` must answer **401**, and so must the same request with a forged header, for example `-H "X-MS-CLIENT-PRINCIPAL: $(echo -n '{"claims":[]}' | base64)"` (Container Apps strips client-supplied copies). Make these two checks part of every deployment pipeline.
4. Restrict who can disable authentication (RBAC on the container app), and alert on `Microsoft.App/containerApps/authConfigs` writes in the Activity Log.

Residual risk, recorded in ADR-0015 and the threat model: the app does not validate bearer tokens itself, so an exposed app without Easy Auth is fully trusting. Validating the token in the app (signature, issuer, audience) is a follow-up. `allowedClientApplications` and `allowedIpRanges` narrow who can reach the API in the meantime, and `vnet` or `internal` remove the exposure.

**Restricting client applications.** `allowedClientApplications` is a list of client application ids that Easy Auth lets through (it checks the token's `azp`/`appid`). Empty (default) means any application can call with a valid token for this API, for example a third-party app that a user consented to. **Recommended in production:** list the clients people use (the UI, Azure CLI `04b07795-8ddb-461a-bbee-02f9e1bf7b46` for operator testing) and the client ids of the automation identities (a managed identity's client id is its application id). A caller whose application is not listed gets 403 from Easy Auth before the app sees the request.

**Health endpoints.** By default (`anonymousHealthEndpoints = true`) `/health` and `/health/ready` are excluded from authentication, so uptime checks and smoke tests can call them without a token. `/health` returns the version, `fabric_mode`, `reasoner` and the time, and reads no caller identity. It does not reveal whether writes are enabled: that flag is on `GET /system/info`, which needs a FabricOps role. Container Apps documents `excludedPaths` as "the paths for which unauthenticated flow would not be redirected to the login page" and does not say whether the match is exact or by prefix, so the app does not rely on it: every other route requires the identity header and returns 401 without it, and no route under `/health` reads it. Set `anonymousHealthEndpoints = false` if version and mode disclosure to the internet matters more than anonymous probes (container health probes are unaffected, they bypass authentication). Smoke tests can then authenticate with an identity holding `FabricOps.Automation`. `/health/ready` answers `200`, or `503` with a fixed reason code (`starting` or `build_failed`) and the exception *type* of the last build failure, never its message (paths and other detail stay in the logs), so anonymous callers learn nothing about the deployment from it.

**Trust boundary.** With `ORQENTIS_API_AUTH_MODE=easy_auth` the app trusts the `X-MS-CLIENT-PRINCIPAL` header. Container Apps authentication sets that header and strips any client-supplied copy, so the app must only be reached through Container Apps ingress. Do not expose additional ports. Keep the environment dedicated to FabricOps, without other or untrusted workloads. Health probes target the container port directly and are not subject to authentication.

**Request limits.** The app rejects bodies over `apiMaxBodyBytes` with 413 and rate-limits each **authenticated** principal with 429 and `Retry-After`: approve, reject and execute (`apiRateLimitMutatingPerMinute`) and the expensive calls, telemetry collection, investigations and audit verification (`apiRateLimitExpensivePerMinute`). The budget is charged after authentication on the validated principal id, so a forged header on `/health` cannot spend anyone's budget. The limits live in memory **per replica**; with `maxReplicas = 1` they are exact. Reads are not limited by the app. Put Azure Front Door or API Management in front if the API needs a global limit, WAF rules or request logging at the edge.

## MVP storage and its limits

The MVP keeps its stores on the Azure Files share mounted at `/var/lib/orqentis`:

* `events.sqlite3`: event store
* `state.sqlite3`: incidents, approvals, dependencies
* `evidence/`: evidence and incident reports
* `audit/audit.jsonl`: hash-chained audit log
* `logs/`
* `contracts/`

Constraints that follow from this:

* **Single writer.** SQLite and the hash-chained audit log allow exactly one writing process, and the scheduler's leader lease is in-process. The app is therefore pinned to `minReplicas = maxReplicas = 1`, and uvicorn runs a single worker. Do not scale it out. In single-revision mode, Container Apps briefly runs the old and new revision side by side during an update. Deploy changes in a quiet period, and run `orqentis audit` / `GET /audit/verify` afterwards.
* **SMB and SQLite.** The share is mounted with `nobrl`, so byte-range locks are not forwarded to the server. SQLite's locking cannot work over SMB, and without `nobrl` it typically fails with "database is locked". This is safe only because there is a single writer. The app uses SQLite WAL mode, which SQLite does not support across hosts on network file systems; with one host it works, but it is not a durable production design.
* **Shared key.** Container Apps mounts Azure Files over SMB with the storage account key, so `allowSharedKeyAccess` must stay `true` on this account. Mitigations: the account is dedicated to this share, TLS 1.2+ and HTTPS-only are enforced, public blob access and cross-tenant replication are disabled, the portal defaults to Entra auth, and soft delete for shares is on. Rotate the key with `az storage account keys renew`, then redeploy the template so the environment picks up the new key. For a private setup, add a private endpoint and a VNet-integrated environment.
* **Mount ownership.** The mount maps all files to UID/GID `10001` with owner-only modes, matching the non-root user in the Dockerfile. Change both together.

**Target architecture.** These local stores are replaced by **Eventhouse** (KQL) for operational events, telemetry and the audit trail, and by **OneLake** for evidence and reports. The app then becomes stateless, can scale beyond one replica, and no longer needs the file share or a shared key.

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

The `env/*.bicepparam` files read the image, registry and API client id from environment variables (`CONTAINER_IMAGE`, `ACR_LOGIN_SERVER`, `ACR_RESOURCE_ID`, `ENTRA_API_CLIENT_ID`, and optionally `FABRIC_MODE`) with no defaults; `scripts/validate-bicep.sh` supplies placeholders. CI (`.github/workflows/ci.yml`) runs the build steps and `scripts/validate-bicep.sh` on every push and pull request, and `.github/workflows/bicep-diff.yml` posts an offline diff of what each environment would deploy (`bicep snapshot`, no Azure access) on pull requests. The deploy job records the real `az deployment group what-if` with the environment's own deploy identity just before it deploys.
