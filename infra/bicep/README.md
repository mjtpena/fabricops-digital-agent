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
| `ORQENTIS_API_MAX_BODY_BYTES` | `apiMaxBodyBytes` (default `65536`): larger request bodies get 413 |
| `ORQENTIS_API_RATE_LIMIT_MUTATING_PER_MINUTE` | `apiRateLimitMutatingPerMinute` (default `30`), per principal and replica |
| `ORQENTIS_API_RATE_LIMIT_EXPENSIVE_PER_MINUTE` | `apiRateLimitExpensivePerMinute` (default `6`): collection, investigations, audit verification |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | Container App **secret** reference |

Not set by the template, because the safe default already applies under `easy_auth`: `ORQENTIS_API_REQUIRE_READ_ROLE` (every call needs a FabricOps role; see section 1) and `ORQENTIS_API_RATE_LIMIT_ENABLED` (`true`).

## Deploy

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
3. **Power BI semantic model refresh history.** Workspace `Viewer` includes Read on the semantic models in that workspace. For a semantic model in a workspace where you do not grant Viewer, give the reader identity **Read** on that semantic model only (**Manage permissions**, or `POST https://api.powerbi.com/v1.0/myorg/groups/{groupId}/datasets/{datasetId}/users` with `{"identifier": "<readerIdentityPrincipalId>", "principalType": "App", "datasetUserAccessRight": "Read"}`).
4. **Capacities and deployment pipelines.** Do not give the reader identity capacity roles or deployment-pipeline Admin. Both carry write permissions: assigning workspaces to a capacity, or deploying. Without them, capacity listing and deployment-state lookups return nothing for that identity. CU utilisation is not available from any public API anyway; ingest Capacity Metrics into Eventhouse instead.
5. **Actor identity: `Contributor` only on workspaces where writes are permitted.** Do this only once a write executor is in use and `enableWriteActions = true`. Use the same portal flow or REST call with `"role": "Contributor"`. Never grant `Admin` or `Member`. Never grant it on workspaces outside the write scope.

**Verify.** As an operator, call `POST /telemetry/collect` and check that `collector_errors` is empty. A 401/403 entry names the API and workspace that still needs a grant.

> In this MVP the REST adapter is **read-only**. With `fabricMode = 'rest'`, approved actions are executed by `DisabledFabricWriteClient`, which refuses them even when `enableWriteActions = true`. The actor identity and the switch exist so that a future write executor can be enabled without touching the read path. Keeping the two identities separate means a compromised read path holds no write token. The strongest form of that separation is to run the write executor as its own app with only the actor identity attached.

### 6. Azure OpenAI (optional, `reasoner = 'agent_framework'`)

Set `azureOpenAiEndpoint`, `azureOpenAiDeployment` and `azureOpenAiResourceId`. The template grants the reader identity **Cognitive Services OpenAI User** on that account, which permits inference only (no keys, no deployments). The grant is made through `modules/openai-role.bicep`, scoped to the account's own resource group and subscription. The deploying principal needs permission to create role assignments there. Without endpoint and deployment the app refuses to start with `agent_framework`.

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

**Health endpoints.** By default (`anonymousHealthEndpoints = true`) `/health` and `/health/ready` are excluded from authentication, so uptime checks and smoke tests can call them without a token. `/health` returns the version, `fabric_mode`, `reasoner` and the time, and reads no caller identity. It does not reveal whether writes are enabled: that flag is on `GET /system/info`, which needs a FabricOps role. Container Apps documents `excludedPaths` as "the paths for which unauthenticated flow would not be redirected to the login page" and does not say whether the match is exact or by prefix, so the app does not rely on it: every other route requires the identity header and returns 401 without it, and no route under `/health` reads it. Set `anonymousHealthEndpoints = false` if version and mode disclosure to the internet matters more than anonymous probes (container health probes are unaffected, they bypass authentication). Smoke tests can then authenticate with an identity holding `FabricOps.Automation`.

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
for f in infra/bicep/main.bicepparam infra/bicep/examples/*.bicepparam; do
  az bicep build-params --file "$f" --stdout > /dev/null
done
```

CI (`.github/workflows/ci.yml`) runs the build steps on every push and pull request.
