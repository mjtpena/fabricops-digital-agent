# Security

FabricOps can influence production data platforms, so it is designed around one rule: **the agent
can look, but it cannot touch.** Every change must pass a deterministic policy engine and an
explicit human approval bound to the exact action. On top of that, the deployment must have its
write capability turned on.

## 1. Identities and capability boundaries

| Identity | Used by | Permissions | Notes |
|---|---|---|---|
| Reader (operating identity), e.g. user-assigned managed identity `id-orqentis-reader` | `RestFabricClient` (all reads); Agent Framework chat client | Fabric workspace **Viewer** on monitored workspaces (workspaces, items, job instances and pipeline activity runs; **not** semantic model refresh history, see section 10); Azure OpenAI **Cognitive Services OpenAI User**; AcrPull on the image registry | Least privilege. No write role anywhere. |
| Actor (write capability), e.g. `id-orqentis-actor` | `FabricWriteClient`, held only by the `ActionGateway` | Fabric **Contributor** only on workspaces where approved remediation is allowed | Not attached to the app unless `enableWriteActions` is set at deployment **and** `ORQENTIS_ENABLE_WRITE_ACTIONS=true`. The MVP ships no REST write adapter. |
| Human approvers | REST API (`/approvals/*`) | Entra ID app roles `FabricOps.Operator`, `FabricOps.PlatformAdmin` | Roles arrive in the Easy Auth client principal. |
| The agent | `AGENT_PRINCIPAL` (`agent:FabricOpsInvestigator`) | Tool allowlist from policy | Cannot approve, cannot execute, cannot be impersonated through the API. |

READ and WRITE are separate in code as well: `FabricClient` (read port) has no mutation methods,
and a test (`test_read_client_exposes_no_mutation_operations`) enforces this. Mutation is possible
only through `FabricWriteClient`, which the composition root hands exclusively to the
`ActionGateway`.

Fabric prerequisites for the reader identity:

1. A tenant admin enables *Service principals can use Fabric APIs* (Admin portal, Developer
   settings), scoped to a security group that contains the identity.
2. Grant the identity the workspace **Viewer** role on each monitored workspace (Fabric portal,
   Manage access, or `POST /v1/workspaces/{id}/roleAssignments`).
3. Do **not** grant the reader deployment-pipeline or capacity roles. The roles that make those
   visible also carry write rights (deploying, assigning workspaces to capacities). As a result,
   deployment-state and capacity lookups return nothing in rest mode, and the investigation
   records that gap.

ARM/Bicep cannot assign Fabric workspace roles. See `infra/bicep/README.md` for the step-by-step
grants and for checking them with `POST /telemetry/collect` (`collector_errors` must be empty).

## 2. Authentication (Microsoft Entra ID)

* `fabric/rest/auth.py` builds credentials by `ORQENTIS_AUTH_MODE`:
  * `default`: `DefaultAzureCredential`, using developer sign-in locally and managed identity in Azure.
  * `managed_identity`: `ManagedIdentityCredential` (optionally a specific client id).
  * `workload_identity`: `WorkloadIdentityCredential` (AKS / federated credentials).
  * `service_principal`: `EnvironmentCredential`, which reads `AZURE_CLIENT_*` from the runtime
    environment or a secret store. Never put these in the repo.
* Fabric scope `https://api.fabric.microsoft.com/.default`; Power BI scope
  `https://analysis.windows.net/powerbi/api/.default`. Tokens are cached in memory until five
  minutes before expiry and are never logged.
* The API authenticates callers with Container Apps / App Service built-in authentication
  (`ORQENTIS_API_AUTH_MODE=easy_auth`), which reads the `X-MS-CLIENT-PRINCIPAL` header. The
  container must only be reachable through that front end (internal ingress plus auth config in
  `infra/bicep`). `dev_headers` mode is refused by configuration validation unless
  `ORQENTIS_DEPLOYMENT_ENVIRONMENT=local`.

## 3. No credentials in the repository

* All configuration comes from environment variables or `.env`, which is git-ignored;
  `.env.example` contains no secrets.
* The Application Insights connection string is a Container App secret.
* The LLM uses Entra ID, not API keys.

## 4. Redaction

`observability/redaction.py` removes the following before anything is logged, stored as evidence,
written to the audit trail or sent to a model:

* bearer tokens and JWTs;
* `client_secret=`, `password=`, `api_key=`, `token=`, storage `AccountKey=` and SAS signatures;
* values of sensitive keys (`secret`, `password`, `token`, `credential`, `connection_string`, ...).

The auth-failure scenario includes a real-looking secret inside a Fabric error message. Tests
assert that it never reaches evidence, the report or tool output.

## 5. Structured audit trail

`observability/audit.py` is an append-only JSONL log in which each record holds the SHA-256 of its
predecessor. `orqentis audit` and `GET /audit/verify` detect modified, reordered or deleted
records. The following are audited:

* every tool invocation (caller, arguments, status, latency);
* every policy evaluation (rules matched, risk, required role, policy version);
* approval requests, decisions, refusals, expiries and cancellations;
* incident creation and every status transition;
* agent decisions (reasoner, model, prompt version, token usage, rejected outputs);
* execution attempts, including every blocked attempt and its reason.

The target architecture ships audit records to an append-only Eventhouse table or immutable
storage.

## 6. Tool allowlists and input validation

* The agent may call only the tools listed in the policy's `agent_tool_allowlist`. Approval and
  execution tools do not exist.
* Every tool input is a Pydantic model with `extra="forbid"`, length limits and an id pattern
  (`^[A-Za-z0-9._:-]+$`), so path traversal, extra parameters and oversized inputs are rejected
  before the handler runs.
* The agent has a hard per-investigation tool budget, reserved before any `await` so that
  concurrent calls cannot overshoot it, and each tool has a timeout.
* API request bodies and query parameters are validated by FastAPI/Pydantic.

## 7. Approval before mutation

* Mutation-request tools call only `ActionGateway.submit`. That returns `ACTION_REQUIRES_APPROVAL`
  (and opens a PENDING approval) or `ACTION_DENIED`. It never executes anything.
* The policy engine prohibits `DELETE_ITEM` and production `CHANGE_PERMISSIONS`. HIGH_RISK
  actions need `FabricOps.PlatformAdmin`. A code invariant ensures no policy file can auto-allow a
  mutation in the MVP.
* The approval service rejects decisions from agents and services, from the requester, from
  callers without the required role, on expired approvals, and on approvals that have already
  been decided.
* Execution requires all of the following:
  * the approval is APPROVED and unexpired;
  * a human principal holds the required role;
  * the action fingerprint is unchanged;
  * the action has not already been executed;
  * the policy does not deny it now;
  * the write capability is enabled.

The critical tests in `tests/scenarios/test_security_boundary.py` and
`tests/integration/test_agent_framework_reasoner.py` exercise these paths, including a model that
obeys an injected instruction.

## 8. Prompt-injection resistance at the tool boundary

Fabric metadata, telemetry, item descriptions, incident history and runbook text are **untrusted
input**:

* Before reaching the model, tool output is sanitised: control and bidi characters are stripped,
  envelope-like tags are neutralised and strings are truncated. It is then wrapped in an
  `untrusted_data` envelope with an explicit notice.
* A heuristic scanner flags instruction-like content (`instruction_override`,
  `role_reassignment`, `approval_manipulation`, `tool_invocation`, `destructive_request`). Flags
  are stored on the evidence, shown to the model and surfaced in the report. The scanner is
  defence in depth only.
* The real controls do not depend on the model behaving: the allowlist, the policy engine, the
  approval boundary and grounding validation. Retrieved content cannot alter any of them, because
  they are code and configuration the model cannot reach.
* Runbook procedures are retrieved verbatim from version-controlled files and recommended by id.
  The agent cannot introduce a runbook that was not retrieved.

The synthetic estate contains a deliberately poisoned item description (`nb-transform-sales-dev`)
to keep these defences under test.

## 9. Known gaps (MVP)

* The local stores (SQLite, filesystem) are single-node, and their encryption at rest depends on
  the host volume. The target is Eventhouse plus OneLake, with Entra ID RBAC.
* `easy_auth` trusts the front end's header. Do not expose the container without the built-in
  authentication layer.
* There is no rate limiting on the internal API, which is internal ingress only.
* No REST write adapter is shipped. Enabling writes against a real tenant first requires
  implementing and reviewing one.

## 10. Least-privilege findings from the live sandbox

Measured on 2026-10-20 against the FabricOps sandbox estate (`docs/live-environment.md`) with the test
service principal `fabricops-live-reader`, which holds the workspace **Viewer** role on the four sandbox
workspaces and nothing else. The evidence is in `tests/live` (skipped unless `ORQENTIS_LIVE_TESTS=1`);
`scripts/live-estate/create.sh` rebuilds the estate and the grants.

| Read | Workspace Viewer alone | Notes |
|---|---|---|
| List workspaces (`GET /v1/workspaces`) | Works | Returns only the workspaces the principal holds a role on. |
| List and get items | Works | A lakehouse also appears as a second item of type `SQLEndpoint` with the same name; the adapter keeps it as a generic item. |
| Job instances of pipelines and notebooks | Works | Returned newest first. |
| Pipeline activity runs (`POST .../queryactivityruns`, a read-only query) | Works | Carries the activity error text. A timed-out activity has an empty error object; the adapter now says "timed out". |
| Semantic model metadata (`GET .../datasets/{id}`, list datasets and reports) | Works | |
| **Semantic model refresh history** (`GET .../datasets/{id}/refreshes`) | **Denied: 403 `Unauthorized`** | Power BI requires **Write** permission on the model for the refresh *list* ([documented limitation](https://learn.microsoft.com/en-us/rest/api/power-bi/datasets/get-refresh-history-in-group): "Caller must have Write permissions on the dataset"). No read-only dataset grant changes that (see "Refresh history: what was tried" below), so the programme did **not** grant anything: a Viewer-only FabricOps cannot list refresh outcomes. The refresh collector records the 403 in `collector_errors` and the other collectors carry on. Decision for the owner: accept the gap, give the principal workspace Contributor (the only way to get dataset Write; a real privilege increase), or take refresh outcomes from another source. One narrow read path does exist for a *known* refresh id: see below. |
| List capacities (`GET /v1/capacities`) | Works, but is not what it seems | Returns only capacities that are open to everyone (in the sandbox tenant a trial capacity and a PPU capacity). The dedicated F-SKU sandbox capacity is invisible. Capacity roles carry write rights, so they stay ungranted. |
| Deployment pipelines | Empty list | No deployment-pipeline role is granted (the roles that expose them can deploy). |
| OneLake (`_delta_log`, tables, files) | **Denied: 403 on every path** | See below. |

### Reading a table's `_delta_log` over OneLake (schema-drift collector, ADR-0009)

Workspace Viewer gives no OneLake data access. The narrowest working read-only grant is a **OneLake data
access role** on the source lakehouse that gives the principal `Read` on one table. The call replaces the
lakehouse's whole role set, so send the built-in `DefaultReader` role back unchanged:

```bash
az rest --method put --resource https://api.fabric.microsoft.com \
  --url "https://api.fabric.microsoft.com/v1/workspaces/<workspaceId>/items/<lakehouseId>/dataAccessRoles" \
  --body @roles.json
```

```json
{"name": "fabricopsDeltaLogReader", "kind": "Policy",
 "decisionRules": [{"effect": "Permit", "permission": [
   {"attributeName": "Action", "attributeValueIncludedIn": ["Read"]},
   {"attributeName": "Path", "attributeValueIncludedIn": ["Tables/customers"]}]}],
 "members": {"microsoftEntraMembers": [{"tenantId": "<tenantId>", "objectId": "<principalObjectId>"}]}}
```

Proven with the test principal on the sandbox DEV lakehouse (`tests/live/test_live_onelake.py`, with the TEST
lakehouse as the denied control):

* before the role, listing `Tables/customers/_delta_log` and reading `00000000000000000000.json` were both
  HTTP 403; after it both were HTTP 200, effective immediately; removing the role made them 403 again;
* the grant is read-only and table-scoped: the other table (`customers_clean`) and `Files` stay 403, and
  creating a file under `Files` or `Tables/customers` stays 403;
* role names allow only letters and digits, and roles can be defined only at the lakehouse root or table
  level, so `_delta_log` cannot be granted on its own: reading the log also lets the identity read that
  table's data files;
* no write right, item-level `Write` or Contributor role is involved, and the role goes away with the
  lakehouse.

The brief expected item-level `Read` + `ReadAll`. `ReadAll` exposes every table and all files, so the
table-level role is the smaller grant, and there was nothing to stop on. Whether the role also works without
the Viewer assignment could not be isolated: after the Viewer assignment was deleted, Fabric kept honouring
it for the service principal for at least 16 minutes (workspace, items, runs and activity runs stayed
readable). Grant both, and do not assume that removing a service principal's workspace role takes effect
immediately.

### Refresh history: what was tried (2026-10-20, sandbox `customers_model`, test principal)

| Attempt | Result |
|---|---|
| Baseline: `GET .../datasets/{id}/refreshes?$top=5` with workspace Viewer | 403 at 02:22:51Z and again at 02:24:03Z, 02:24:22Z; body `{"error":{"code":"Unauthorized","message":"Api accessed by user <eupi>{principal object id}</eupi> with insufficient privileges. request is unauthorized, identity None."}}`. The sandbox owner reads the same endpoint with 200. |
| (a) Dataset `Read` granted directly (`POST .../datasets/{id}/users`, `principalType: App`) | **Not possible and not needed.** 400 `InvalidRequest`: "API supported only for User or Group principal types" (02:23:17Z; the docs say the same: adding permissions to service principals is not supported). Not needed because `GET .../users` already lists the principal with `datasetUserAccessRight: Read`, derived from the Viewer role, and the 403 above is what Read gets. |
| (b) `ReadExplore` (Build), `POST` and `PUT` | Same 400 for both (02:23:37Z, 02:23:39Z). Build cannot be set on a service principal through the API. Per the docs Viewer has Read but not Build, and Build is "build new content" and XMLA querying, not refresh history. A Build grant for a group would need an Entra group, which is out of scope. Not tested further. |
| (c) Fabric API, `GET /v1/workspaces/{ws}/items/{id}/jobs/instances` for the SemanticModel item | 404 `ItemNotFound` "Could not found the requested item" for the test principal **and** for the owner (02:23:39Z): the job API does not support semantic models at all. |
| Other Power BI reads | `GET /v1.0/myorg/datasets/{id}/refreshes` (no group): 403 "API is not accessible for application". `GET .../datasets/{id}` metadata: 200. `GET .../users`, `.../refreshSchedule`: 401 / 404. |
| **`GET .../datasets/{id}/refreshes/{requestId}`** ([Get Refresh Execution Details](https://learn.microsoft.com/en-us/rest/api/power-bi/datasets/get-refresh-execution-details-in-group)) | **200 with Viewer only**, for a completed refresh (02:24:43Z), a Direct Lake framing refresh (02:24:53Z) and a **failed** one (02:26:31Z, with the engine `messages` and `serviceExceptionJson`, i.e. the error text). It needs the refresh's request id, and there is no way to list ids as a Viewer. |

No dataset or workspace grant was made or changed by this investigation. The permission that does open the list is dataset **Write**
([semantic model permissions](https://learn.microsoft.com/en-us/power-bi/connect-data/service-datasets-permissions): Write is implicit
for workspace Admin, Member and Contributor, never for Viewer, and the REST API "can't be used to grant dataset Write"). For a service
principal that means the Contributor workspace role, which also lets it create and change every item in the workspace.

The execution-details endpoint is the only read-only route to refresh outcomes. It is usable where something else already knows the
request id (a refresh started by a pipeline's semantic model refresh activity, whose run output should carry it: not verified here,
because that activity needs a Power BI connection that the sandbox deliberately does not create), not for scheduled or manual
refreshes.

### Failure-injection facts that affect detection

* A Fabric connection of type `WebForPipeline` (used by pipeline Web activities) cannot skip its connection
  test but accepts any HTTP answer, so a deliberately invalid credential can be stored for failure testing.
* The pipeline Web activity reports the body of a failed call, not its status code. A 401 from the Fabric API
  surfaces as "Unable to process the request" and is not recognisable as an authentication failure; a call to
  the Microsoft Entra token endpoint with a bad client surfaces the `AADSTS` text, which is.
* A notebook that raises surfaces as pipeline error code 2451 with the exception name and text nested in the
  message; a timed-out activity surfaces as pipeline error code 500 "InternalServerError executing request".
