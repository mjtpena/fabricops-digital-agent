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
3. **Power BI semantic model refresh history.** Workspace `Viewer` includes Read on the semantic models in that workspace. For a semantic model in a workspace where you do not grant Viewer, give the reader identity **Read** on that semantic model only (**Manage permissions**, or `POST https://api.powerbi.com/v1.0/myorg/groups/{groupId}/datasets/{datasetId}/users` with `{"identifier": "<readerIdentityPrincipalId>", "principalType": "App", "datasetUserAccessRight": "Read"}`).
4. **Capacities and deployment pipelines.** Do not give the reader identity capacity roles or deployment-pipeline Admin. Both carry write permissions: assigning workspaces to a capacity, or deploying. Without them, capacity listing and deployment-state lookups return nothing for that identity. CU utilisation is not available from any public API anyway; ingest Capacity Metrics into Eventhouse instead.
5. **Actor identity: `Contributor` only on workspaces where writes are permitted.** Do this only once a write executor is in use and `enableWriteActions = true`. Use the same portal flow or REST call with `"role": "Contributor"`. Never grant `Admin` or `Member`. Never grant it on workspaces outside the write scope.

**Verify.** As an operator, call `POST /telemetry/collect` and check that `collector_errors` is empty. A 401/403 entry names the API and workspace that still needs a grant.

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
az bicep build-params --file infra/bicep/examples/cross-tenant.bicepparam --stdout > /dev/null
```

CI (`.github/workflows/ci.yml`) runs the build steps on every push and pull request.
