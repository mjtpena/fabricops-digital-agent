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
| Container App | `ca-<prefix>` | The API (`orqentis serve`), port 8000, 1 replica, Entra ID authentication |
| Container App auth config | `current` | Built-in authentication (Easy Auth) with Microsoft Entra ID, `Return401` |
| Role assignment (optional) | n/a | `AcrPull` for the reader identity on an ACR in the same RG (`containerRegistryName`) |
| Role assignment (optional) | `modules/openai-role.bicep` | `Cognitive Services OpenAI User` for the reader identity on `azureOpenAiResourceId` (any RG/subscription) |

The app runs with these settings (see `src/orqentis/config/settings.py`):

| Variable | Value |
|---|---|
| `ORQENTIS_DEPLOYMENT_ENVIRONMENT` | `deploymentEnvironment` (default `prod`) |
| `ORQENTIS_TENANT_ID` | `fabricTenantId` (default: the deployment tenant; required in rest mode) |
| `ORQENTIS_API_AUTH_MODE` | `easy_auth` (required outside `local`) |
| `ORQENTIS_AUTH_MODE` | `managed_identity` |
| `ORQENTIS_MANAGED_IDENTITY_CLIENT_ID`, `AZURE_CLIENT_ID` | reader identity client id |
| `ORQENTIS_FABRIC_MODE` | `fabricMode` (default `rest`) |
| `ORQENTIS_CONTRACTS_PATH` | `contractsPath` (default `/var/lib/orqentis/contracts/estate.json`) |
| `ORQENTIS_ENABLE_WRITE_ACTIONS` | `enableWriteActions` (default `false`) |
| `ORQENTIS_REASONER` | `reasoner` (default `rule_based`) |
| `ORQENTIS_AZURE_OPENAI_ENDPOINT` / `_DEPLOYMENT` | `azureOpenAiEndpoint` / `azureOpenAiDeployment` (only set when non-empty) |
| `ORQENTIS_DATA_DIR` | `/var/lib/orqentis` (Azure Files mount) |
| `ORQENTIS_LOG_FORMAT` | `json` |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | Container App **secret** reference |

## Deploy

Prerequisites: Azure CLI with Bicep (`az bicep install`), permission to create resources and role assignments in the target resource group (Owner, or Contributor + Role Based Access Control Administrator), and the ability to create an Entra app registration.

### 1. Register the API in Microsoft Entra ID

Container Apps authentication validates bearer tokens issued for this app registration. It never runs an interactive login, so no client secret is needed.

```bash
API_APP_ID=$(az ad app create --display-name "Orqentis FabricOps API" \
  --sign-in-audience AzureADMyOrg --query appId -o tsv)
az ad app update --id "$API_APP_ID" --identifier-uris "api://$API_APP_ID"
# v2 access tokens: their issuer matches the v2.0 openIdIssuer the template configures.
az ad app update --id "$API_APP_ID" --set api.requestedAccessTokenVersion=2

# App roles used by the policy engine (src/orqentis/policies/default_policy.yaml).
cat > app-roles.json <<EOF
[
  {"allowedMemberTypes": ["User"], "isEnabled": true, "id": "$(uuidgen)",
   "value": "FabricOps.Operator", "displayName": "FabricOps Operator",
   "description": "Investigate incidents and approve production and non-production write actions."},
  {"allowedMemberTypes": ["User"], "isEnabled": true, "id": "$(uuidgen)",
   "value": "FabricOps.PlatformAdmin", "displayName": "FabricOps Platform Admin",
   "description": "Everything an operator can do, plus approve HIGH_RISK actions (capacity, deployments, permissions)."}
]
EOF
az ad app update --id "$API_APP_ID" --app-roles @app-roles.json

# Only users and groups assigned to the app can get a token for it.
az ad sp create --id "$API_APP_ID"
az ad sp update --id "$API_APP_ID" --set appRoleAssignmentRequired=true
```

Then, in the portal under **App registrations > Orqentis FabricOps API > Expose an API**:

* Add a delegated scope, e.g. `FabricOps.Access`.
* Add the client applications people use to call the API as authorized clients. For example, add Azure CLI (`04b07795-8ddb-461a-bbee-02f9e1bf7b46`) for operator testing with `az account get-access-token --scope api://<API_APP_ID>/FabricOps.Access`.

**Assign the app roles to approvers.** Use **Enterprise applications > Orqentis FabricOps API > Users and groups > Add user/group**, choose `FabricOps.Operator` or `FabricOps.PlatformAdmin`, and select the people or groups who may approve actions. Assigning groups needs Entra ID P1/P2. The assigned roles appear in the access token's `roles` claim. Container Apps authentication forwards that claim in `X-MS-CLIENT-PRINCIPAL`, and `orqentis.api.auth` turns it into the caller's roles.

* `FabricOps.Operator` can approve LOW/MEDIUM-risk actions.
* `FabricOps.PlatformAdmin` is also required for HIGH_RISK actions.
* Read endpoints only require an authenticated caller; approvals are checked against these roles.

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

When `containerRegistryName` names an ACR in the same resource group, the template grants the reader identity `AcrPull` and pulls with it. For a registry elsewhere, set `containerRegistryServer` and grant `AcrPull` to the `readerIdentityPrincipalId` output yourself. The first revision fails to pull until that grant exists. After granting, restart the revision.

### 4. Upload the estate contracts (rest mode)

In `rest` mode the app needs the declared topology, data products and SLAs at `contractsPath`. Until that file exists the container exits at startup and Container Apps keeps restarting it. The file format is the same as `fixtures/fabric/estate.json`: a full estate file, or just `external_sources`, `data_products`, `dependencies` and `slas`.

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
3. **Power BI semantic model refresh history is not readable with Viewer.** Workspace `Viewer` includes Read on the semantic models in that workspace (the identity can read the model's metadata), but `GET /v1.0/myorg/groups/{groupId}/datasets/{datasetId}/refreshes` returns **403 `Unauthorized`** for it: Power BI requires write permission on the model for refresh history. Measured live on the sandbox estate (`tests/live/test_live_rest.py::test_refresh_history_needs_more_than_viewer`; findings in `docs/security.md` section 10). There is no read-only dataset grant that fixes this: dataset `Read` is what Viewer already has, `Build` and `Read` cannot be assigned to a service principal through the REST API at all ("API supported only for User or Group principal types"), and the Fabric job-instances API does not cover semantic models (404). The only permission that opens the list is dataset **Write** (workspace Contributor or higher), so **do not grant write to make it work**. The one read-only route is `GET .../datasets/{datasetId}/refreshes/{requestId}` (Get Refresh Execution Details, 200 with Viewer, includes the failure messages), which needs the refresh's request id and cannot list them. Details and timestamps: `docs/security.md` section 10. With Viewer only, `POST /telemetry/collect` reports `collector_errors["semantic-model-refreshes"]` for each monitored model and every other collector carries on. Accept that gap, or decide explicitly to raise the privilege or to source refresh outcomes elsewhere.
4. **Capacities and deployment pipelines.** Do not give the reader identity capacity roles or deployment-pipeline Admin. Both carry write permissions: assigning workspaces to a capacity, or deploying. Without them, capacity listing and deployment-state lookups return nothing for that identity. CU utilisation is not available from any public API anyway; ingest Capacity Metrics into Eventhouse instead.
   **OneLake `_delta_log` reads (schema-drift collector, ADR-0009).** Workspace `Viewer` gives no OneLake access (every path is 403). The smallest working grant is a *OneLake data access role* with `Read` on one table, created per source lakehouse with `PUT https://api.fabric.microsoft.com/v1/workspaces/{workspaceId}/items/{lakehouseId}/dataAccessRoles` (the call replaces the whole role set, so include the built-in `DefaultReader` role unchanged). Roles exist only at lakehouse-root and table level, so the log cannot be granted without the table's data. It is read-only: other tables, `Files` and every write stay 403. The role body, the proof and the limits are in `docs/security.md` section 10; `scripts/live-estate/estate.sh` (`ensure_table_reader_role`) is a working example.
5. **Actor identity: `Contributor` only on workspaces where writes are permitted.** Do this only once a write executor is in use and `enableWriteActions = true`. Use the same portal flow or REST call with `"role": "Contributor"`. Never grant `Admin` or `Member`. Never grant it on workspaces outside the write scope.

**Verify.** As an operator, call `POST /telemetry/collect` and check that `collector_errors` is empty. A 401/403 entry names the API and workspace that still needs a grant.

> In this MVP the REST adapter is **read-only**. With `fabricMode = 'rest'`, approved actions are executed by `DisabledFabricWriteClient`, which refuses them even when `enableWriteActions = true`. The actor identity and the switch exist so that a future write executor can be enabled without touching the read path. Keeping the two identities separate means a compromised read path holds no write token. The strongest form of that separation is to run the write executor as its own app with only the actor identity attached.

### 6. Azure OpenAI (optional, `reasoner = 'agent_framework'`)

Set `azureOpenAiEndpoint`, `azureOpenAiDeployment` and `azureOpenAiResourceId`. The template grants the reader identity **Cognitive Services OpenAI User** on that account, which permits inference only (no keys, no deployments). The grant is made through `modules/openai-role.bicep`, scoped to the account's own resource group and subscription. The deploying principal needs permission to create role assignments there. Without endpoint and deployment the app refuses to start with `agent_framework`.

## Network exposure

| `externalIngress` | `infrastructureSubnetId` | Who can reach the API |
|---|---|---|
| `false` (default) | empty | Only other apps in the same Container Apps environment |
| `true` | empty | The internet. **Every request still needs an Entra token for the API**; unauthenticated callers get 401 |
| `true` | set | Only the VNet (internal environment, no public IP). Front it with Application Gateway/APIM or reach it over VPN/ExpressRoute; create a private DNS zone for the environment's default domain |
| `false` | set | Only other apps in the environment |

The subnet must be delegated to `Microsoft.App/environments` and be at least `/27`, because the environment uses workload profiles.

**Trust boundary.** With `ORQENTIS_API_AUTH_MODE=easy_auth` the app trusts the `X-MS-CLIENT-PRINCIPAL` header. Container Apps authentication sets that header and strips any client-supplied copy, so the app must only be reached through Container Apps ingress. Do not expose additional ports. Keep the environment dedicated to FabricOps, without other or untrusted workloads. Health probes target the container port directly and are not subject to authentication; `/health` exposes only version and mode flags.

## MVP storage and its limits

The MVP keeps its stores on the Azure Files share mounted at `/var/lib/orqentis`:

* `events.sqlite3`: event store
* `state.sqlite3`: incidents, approvals, dependencies
* `evidence/`: evidence and incident reports
* `audit/audit.jsonl`: hash-chained audit log
* `logs/`
* `contracts/`

Constraints that follow from this:

* **Single writer.** SQLite and the hash-chained audit log allow exactly one writing process. The app is therefore pinned to `minReplicas = maxReplicas = 1`, and uvicorn runs a single worker. Do not scale it out. In single-revision mode, Container Apps briefly runs the old and new revision side by side during an update. Deploy changes in a quiet period, and run `orqentis audit` / `GET /audit/verify` afterwards.
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
az bicep build-params --file infra/bicep/main.bicepparam --stdout > /dev/null
```

CI (`.github/workflows/ci.yml`) runs the build steps on every push and pull request.
