# Security

FabricOps can influence production data platforms, so it is designed around one rule: **the agent
can look, but it cannot touch.** Every change must pass a deterministic policy engine and an
explicit human approval bound to the exact action. On top of that, the deployment must have its
write capability turned on.

## 1. Identities and capability boundaries

| Identity | Used by | Permissions | Notes |
|---|---|---|---|
| Reader (operating identity), e.g. user-assigned managed identity `id-orqentis-reader` | `RestFabricClient` (all reads); Agent Framework chat client | Fabric workspace **Viewer** on monitored workspaces (workspaces, items, job instances and pipeline activity runs; **not** semantic model refresh history, see section 11); Azure OpenAI **Cognitive Services OpenAI User**; AcrPull on the image registry | Least privilege. No write role anywhere. |
| Actor (write capability), e.g. `id-orqentis-actor` | `FabricWriteClient`, held only by the `ActionGateway` | Fabric **Contributor** only on workspaces where approved remediation is allowed | Not attached to the app unless `enableWriteActions` is set at deployment **and** `ORQENTIS_ENABLE_WRITE_ACTIONS=true`. The MVP ships no REST write adapter. |
| Human approvers | REST API (`/approvals/*`) | Entra ID app roles `FabricOps.Operator`, `FabricOps.PlatformAdmin` | Roles arrive in the Easy Auth client principal. |
| Automation identities (scheduler, smoke test) | REST API (read endpoints, `POST /telemetry/collect`) | Entra ID app role `FabricOps.Automation` (Application members only) | An app-only token is a `SERVICE` principal. It can never approve, reject, execute or investigate. |
| Read-only users | REST API (read endpoints) | Entra ID app role `FabricOps.Reader` | Cannot trigger anything. |
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
grants and for checking them with `POST /telemetry/collect` (`collector_errors` must be empty, apart from the `semantic-model-refreshes` 403 a Viewer-only identity gets; section 11).

## 2. Authentication (Microsoft Entra ID)

* `fabric/rest/auth.py` builds credentials by `ORQENTIS_AUTH_MODE`:
  * `default`: `DefaultAzureCredential`, using developer sign-in locally and managed identity in Azure.
  * `managed_identity`: `ManagedIdentityCredential` (optionally a specific client id).
  * `workload_identity`: `WorkloadIdentityCredential` (AKS / federated credentials).
  * `service_principal`: `EnvironmentCredential`, which reads `AZURE_CLIENT_*` from the runtime
    environment or a secret store. Never put these in the repo. This mode exists for a Fabric tenant that
    is a different Entra tenant from the Azure tenant (a managed identity cannot be granted access
    there). In Azure the secret or the PEM certificate is a **Key Vault reference** that Container Apps
    resolves with the reader managed identity (`infra/bicep`, ADR-0014): it is never a Bicep parameter,
    output or CI variable, and a certificate's private key is mounted as a read-only secret file, not on
    the data share. The reader identity's Key Vault role is assigned on the referenced secret(s) only. The
    deployment fails with a readable reason when the inputs are incomplete or ambiguous, and the app refuses to
    start, naming the missing variable names (never values), if they still are. The LLM credential stays a managed identity
    (`ORQENTIS_LLM_AUTH_MODE=managed_identity`), so the foreign-tenant service principal never signs
    Azure OpenAI calls. Grant that service principal workspace **Viewer** only, like the reader identity.
* Fabric scope `https://api.fabric.microsoft.com/.default`; Power BI scope
  `https://analysis.windows.net/powerbi/api/.default` (public cloud). Sovereign clouds replace them with
  `ORQENTIS_FABRIC_SCOPE` / `ORQENTIS_POWERBI_SCOPE` and set the Entra authority with
  `ORQENTIS_AUTHORITY_HOST` (an https host; passed to every credential that talks to Entra ID). Tokens are
  cached in memory until five minutes before expiry and are never logged.
* **Interactive administrator sign-in** exists in exactly one place: `orqentis contracts scaffold --scanner`
  (a person at a terminal reads Power BI admin Scanner metadata to draft lineage). It is not an auth mode, it
  is not reachable from the application, it refuses non-interactive use, and a test checks that the API
  application never imports it. The application's own identities never hold Fabric admin rights.
* The API authenticates callers with Container Apps / App Service built-in authentication
  (`ORQENTIS_API_AUTH_MODE=easy_auth`), which reads the `X-MS-CLIENT-PRINCIPAL` header. The
  container must only be reachable through that front end (ingress plus auth config in
  `infra/bicep`; the default is external ingress where every request needs an Entra token, see
  `ingressMode`). `dev_headers` mode is refused by configuration validation unless
  `ORQENTIS_DEPLOYMENT_ENVIRONMENT=local`.

### 2.1 Who is calling: people versus applications

Easy Auth validates the token; it does not say whether the caller is a person. `orqentis.api.claims`
classifies every principal from the token claims (ADR-0015 has the full decision table):

* **HUMAN** needs positive evidence of a user token: a delegated access token (`scp` present) or an
  ID-token browser session, in both cases with a pairwise `sub` that differs from `oid`.
* **SERVICE** is any app-only token: `idtyp = app`, `sub == oid`, or a token carrying the
  application-only `FabricOps.Automation` role. These callers keep their roles for reads but are
  never HUMAN.
* **Ambiguous or contradictory claims** (an app signal together with a user signal, unknown `idtyp`,
  conflicting duplicates, no `sub`, no user identity) are refused with `403 PRINCIPAL_AMBIGUOUS`. They
  are never HUMAN, and the app logs the rule that decided.

Only a HUMAN principal can approve, reject or execute. This is checked at the route
(`DecidingPrincipal`, which also refuses any token carrying `FabricOps.Automation` and audits the
refusal), in the approval service and in the action gateway. Grant `FabricOps.Automation` only to
managed identities or service principals (`allowedMemberTypes: ["Application"]`), and give approver
roles only to people. Both are enforced by the checks above even when Entra is misconfigured.

**`HUMAN` means a user token, not necessarily a person.** Password-signing service accounts, agent users and
third-party applications acting for a consenting user also classify `HUMAN`. Do not assign approver roles to
non-interactive accounts, and list the allowed client applications (`allowedClientApplications`). Exactly one
`X-MS-CLIENT-PRINCIPAL` header is accepted (several: 401), the header is strictly base64, capped in size, and the
payload cannot relabel claims as roles. Cookie sessions (WS8) additionally need CSRF protection (JSON-only
state-changing routes plus an `Origin` check); ADR-0015 records the requirement.

`orqentis serve` refuses `dev_headers` on a non-loopback address unless `ORQENTIS_DEV_HEADERS_ALLOW_REMOTE=true`
is set, and the container image defaults to `easy_auth`, so an unconfigured `docker run` is not an open API.

### 2.2 Roles and what they unlock

| Role | Read endpoints | `POST /telemetry/collect` | `POST /incidents/{id}/investigate` | Approve / reject / execute |
|---|---|---|---|---|
| `FabricOps.Reader` | yes | no | no | no |
| `FabricOps.Automation` | yes | yes | no | never |
| `FabricOps.Operator` | yes | yes | yes | LOW/MEDIUM risk, human only |
| `FabricOps.PlatformAdmin` | yes | yes | yes | HIGH_RISK, human only |

Under Easy Auth a caller needs one of these roles for every endpoint except `/health` (liveness only: it does not
reveal whether writes are enabled; `GET /system/info` does and needs a role). A principal with no role
gets 403. `ORQENTIS_API_REQUIRE_READ_ROLE=false` opens the plain read endpoints only: collection, investigation and
audit verification keep their roles, and approving stays human-only. The interactive docs and `/openapi.json` are
not served under Easy Auth (they would answer callers with no role).

### 2.3 Request limits

* Bodies over `ORQENTIS_API_MAX_BODY_BYTES` (64 KiB by default) get **413**, both when `Content-Length`
  declares too much and when a chunked body grows past the limit.
* Approve, reject and execute (`ORQENTIS_API_RATE_LIMIT_MUTATING_PER_MINUTE`, 30) and expensive calls, telemetry
  collection, investigations and audit verification (`ORQENTIS_API_RATE_LIMIT_EXPENSIVE_PER_MINUTE`, 6),
  are limited per **authenticated** principal and answer **429** with `Retry-After`. The budget is charged after
  the caller has been authenticated and keyed on the principal id from the validated claims, never on a raw header,
  so a forged `X-MS-CLIENT-PRINCIPAL` on `/health` or an unrouted path cannot drain anyone's budget. Refused
  attempts are charged too, which bounds the audit records an authenticated caller can cause. Reads are not
  limited by the app. Unauthenticated traffic is rejected by Easy Auth before it reaches the app.
* Limits live in memory per replica (the MVP runs one). A global limit, WAF and edge logging belong
  in Front Door or API Management.

## 3. No credentials in the repository

* All configuration comes from environment variables or `.env`, which is git-ignored;
  `.env.example` contains no secrets.
* The Application Insights connection string is a Container App secret.
* The LLM uses Entra ID, not API keys.
* PostgreSQL accepts an Entra access token only (`passwordAuth` is disabled on the server and no password setting
  exists in the app); Blob storage has shared-key access disabled. The settings that `auth=none` / `ssl=disable`
  would open for a local test container are rejected outside `deployment_environment=local`.
* The contracts file holds declarations, not credentials. Its validation and loading errors name
  paths, ids and positions and never echo values from the file. `contracts scaffold` redacts and
  sanitises what it reads from Fabric (names, data source descriptions) and drops credentials, user info
  and query strings from data source URLs.
* In a deployed environment the contracts file is a blob in a versioned, soft-deleted container that the app can only
  read. The SHA-256 and blob version of the contracts in force are written to the audit trail on every (re)load
  (`contracts.loaded`), so a change to the declared environments is attributable to an upload. A missing, unreadable
  or invalid blob is a typed error that names the blob's address and the violations, never its content; at start-up it
  leaves the app not ready, and on a reload it keeps the last good contracts.
* **A declared environment is a trust decision.** Declaring a PROD workspace as DEV lowers incident
  severity and stops production-only policy rules (`POL-002`, `POL-020`) from matching it. Therefore: only
  *reviewed* entries are enforced (a scaffold draft marked `inferred`, or an `UNKNOWN` entry, never overrides the
  name); every override that changes a classification is logged, the first time the node is classified, and written to the hash-chained audit trail
  with the SHA-256 of the contracts file (`contracts.environment_override`, `contracts.loaded`); and
  `orqentis contracts validate --strict` flags downgrades and catch-all patterns for CI. Review changes to
  `environments` like code. The code-level invariant is unaffected: no mutation is ever auto-allowed in any
  environment.

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

With the PostgreSQL backend (deployed environments) the chain lives in the database, serialised across replicas by a
row lock on the chain head, and `audit_records` rejects `UPDATE`, `DELETE` and `TRUNCATE` with triggers (defence in
depth against bugs). The tamper evidence is the chain plus **anchors**: every 15 minutes the head
(epoch, sequence, hash) is written once to a blob in a container with version-level immutability, and `orqentis
audit` / `GET /audit/verify` check that the database still holds the anchored record with the anchored hash. An
edited record, a removed record, a removed tail, and a table rewritten with every hash recomputed are each detected
(`tests/concurrency/test_audit_chain.py`). Records newer than the latest anchor are protected by the chain alone.
After a restore the database can be behind its anchor; that is reported until an operator records the loss with
`orqentis audit start-epoch` ([ADR-0017](adr/0017-postgresql-system-of-record.md),
[operations/restore.md](operations/restore.md)).

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
  * a human principal holds the required role, and the role the **current** policy requires;
  * the action fingerprint is unchanged;
  * the action has not already been executed: single use is claimed with a compare-and-set persisted
    **before** the write call (concurrent executes reach the write client once), and an unknown outcome keeps
    the claim so a retry cannot repeat the write;
  * the policy does not deny it now, and has not reclassified it to a higher risk than was approved;
  * the write capability is enabled.
* Decisions are atomic: approve, reject, expire and cancel are compare-and-set transitions, so of two concurrent
  decisions exactly one is stored and the other gets 409.

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

* The local stores (SQLite, filesystem) are single-node and for development only: deployed environments refuse to
  start on them. The PostgreSQL and Blob stores are encrypted at rest by the platform (Microsoft-managed keys;
  customer-managed keys are a possible follow-up) and reached over TLS 1.2+ (`verify-full`).
* `easy_auth` trusts the front end's header. Do not expose the container without the built-in
  authentication layer.
* **The app trusts `X-MS-CLIENT-PRINCIPAL` and does not validate bearer tokens itself.** If an externally reachable
  app ever runs without Container Apps authentication (the first deployment before the auth config applies,
  authentication disabled by mistake, a platform fault), a forged header can name a `FabricOps.PlatformAdmin` and be
  believed. Deploy with `ingressMode = 'internal'` first, verify authentication, then switch (infra README, "First
  deployment and the Easy Auth window"), and check in every deployment that tokenless and forged-header requests get 401.
  In-app token validation is the follow-up that removes this dependency (ADR-0015).
* Rate limits are per replica and in memory, and plain reads are not limited (see 2.3). The default ingress is
  external (authenticated); use `allowedIpRanges` or `vnet` where the API must not be internet-reachable.
* Easy Auth's claim-name mapping and the `sub == oid` behaviour of app tokens are handled from documentation
  and synthetic principals. They have not been checked against a live tenant (ADR-0015).
* No REST write adapter is shipped. Enabling writes against a real tenant first requires
  implementing and reviewing one.

## 10. Autonomous operation

The scheduler adds an actor, `system:orqentis` (`PrincipalKind.SERVICE`), that runs collection,
detection, auto-investigation, the SLA sweep and the catalog refresh. It changes nothing about the
boundaries above:

* Detection is still only deterministic `DetectionRule`s over events. The SLA sweep records
  assessments as events; opening an incident from one is a rule's job.
* An automatic investigation is the same workflow with the same allowlisted read-only tools,
  grounding validation, computed confidence and policy evaluation. It can *request* approvals (as the
  agent principal), never approve or execute: those paths still require a human principal with the
  role, and `system:` ids cannot be impersonated over the API.
* Autonomous loops are bounded: a global Fabric concurrency and rate budget, investigation
  concurrency and timeouts, a per-incident retry limit, a batch size per run, and non-overlapping
  jobs. A throttling response from Fabric pauses every caller.
* Job failures are logged with redacted messages and audited when health changes. The readiness
  probe is unauthenticated, so it shows only a fixed reason code and the exception type; messages
  (which can contain file paths) stay in the redacted logs.
* Catalog refreshes are audited with the ids that changed, and an empty estate is refused so a
  permission glitch cannot silently blind monitoring.

**Deploys and several writers.** Container Apps (single-revision mode) runs the old and the new revision
side by side during an update, and test/prod run two replicas. With the PostgreSQL system of record
([ADR-0017](adr/0017-postgresql-system-of-record.md)) that is safe: the audit chain is serialised by the database
(one valid chain from any number of writers), approvals are decided and executed with compare-and-set (at most one
execution per approval), the leader lease uses the database clock and every scheduled write is fenced by the lease
generation (a deposed leader's late write is refused), detection runs under a cross-replica mutex, and investigation
jobs are durable rows that any replica claims. The start delay (`schedulerStartupDelaySeconds`) is now only a
courtesy. Tested with two instances, two OS processes, a killed process and a graceful stop
(`tests/concurrency`).

Limits: the incident aggregate is still a last-writer-wins upsert (a row version is the follow-up), and rate limits
are per replica (section 2.3). Detection and manual collection wait for each other for up to two minutes.

## 11. Least-privilege findings from the live sandbox

Measured on 2026-10-20 against the FabricOps sandbox estate (`docs/live-environment.md`) with the test
service principal `fabricops-live-reader`, which holds the workspace **Viewer** role on the four sandbox
workspaces and nothing else. The evidence is in `tests/live` (skipped unless `ORQENTIS_LIVE_TESTS=1`);
`scripts/live-estate/create.sh` rebuilds the estate and the grants.

| Read | Workspace Viewer alone | Notes |
|---|---|---|
| List workspaces (`GET /v1/workspaces`) | Works | Returns only the workspaces the principal holds a role on. |
| List and get items | Works | A lakehouse also appears as a second item of type `SQLEndpoint` with the same name; the adapter keeps it as a generic item. |
| Job instances of pipelines and notebooks | Works | Observed newest first (7 runs on 2026-10-20, asserted by `test_list_item_job_instances_comes_back_newest_first_from_the_service`). Fabric documents no ordering, so the adapter sorts by start time itself and never stops reading pages early. |
| Pipeline activity runs (`POST .../queryactivityruns`, a read-only query) | Works | Carries the activity error text. A timed-out activity has an empty error object; the adapter now says "timed out". |
| Semantic model metadata (`GET .../datasets/{id}`, list datasets and reports in a workspace) | Works (hand probes, not part of the adapter) | Observed HTTP 200 for the test principal with Viewer on 2026-10-20 (`GET .../datasets/{id}` at 02:24:05Z; list datasets and list reports earlier the same day). The adapter does not call these endpoints, so no committed test or fixture covers them. |
| **Semantic model refresh history** (`GET .../datasets/{id}/refreshes`) | **Observed: 403 `Unauthorized`** | **Observed** with Viewer only, on the sandbox model: 403 at 02:22:51Z, 02:24:03Z, 02:24:22Z and 02:28:18Z (committed as a test and a fixture: `test_refresh_history_needs_more_than_viewer`, `error_refresh_history_forbidden.json`). **Tried on 2026-10-20 to fix it, none worked**: dataset `Read` and `ReadExplore` cannot be granted to a service principal (the users API answers 400 "API supported only for User or Group principal types"), the principal's effective dataset right is already `Read`, and the Fabric job-instances API returns 404 for a semantic model, even for the owner (details and timestamps below). Microsoft documents the cause: "Caller must have Write permissions on the dataset" ([Get Refresh History In Group](https://learn.microsoft.com/en-us/rest/api/power-bi/datasets/get-refresh-history-in-group)); that is what the 403 is consistent with, and Contributor was deliberately not tried because it is a write role. Nothing was granted. The refresh collector records the 403 in `collector_errors` and the other collectors carry on. Decision for the owner: accept the gap, give the principal workspace Contributor (the documented way to get dataset Write; a real privilege increase), or take refresh outcomes from another source. One narrow read path works for a *known* refresh id: see below. |
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
| Baseline: `GET .../datasets/{id}/refreshes?$top=5` with workspace Viewer | 403 at 02:22:51Z and again at 02:24:03Z, 02:24:22Z and 02:28:18Z; body `{"error":{"code":"Unauthorized","message":"Api accessed by user <eupi>{principal object id}</eupi> with insufficient privileges. request is unauthorized, identity None."}}`. The sandbox owner reads the same endpoint with 200. |
| (a) Dataset `Read` granted directly (`POST .../datasets/{id}/users`, `principalType: App`) | **Not possible and not needed.** 400 `InvalidRequest`: "API supported only for User or Group principal types" (02:23:17Z; the docs say the same: adding permissions to service principals is not supported). Not needed because `GET .../users` already lists the principal with `datasetUserAccessRight: Read`, derived from the Viewer role, and the 403 above is what Read gets. |
| (b) `ReadExplore` (Build), `POST` and `PUT` | Same 400 for both (02:23:37Z, 02:23:39Z). Build cannot be set on a service principal through the API. Per the docs Viewer has Read but not Build, and Build is "build new content" and XMLA querying, not refresh history. A Build grant for a group would need an Entra group, which is out of scope. Not tested further. |
| (c) Fabric API, `GET /v1/workspaces/{ws}/items/{id}/jobs/instances` for the SemanticModel item | 404 `ItemNotFound` "Could not found the requested item" for the test principal **and** for the owner (02:23:39Z): the job API does not support semantic models at all. |
| Other Power BI reads | `GET /v1.0/myorg/datasets/{id}/refreshes` (no group): 403 "API is not accessible for application". `GET .../datasets/{id}` metadata: 200. `GET .../users`, `.../refreshSchedule`: 401 / 404. |
| **`GET .../datasets/{id}/refreshes/{requestId}`** ([Get Refresh Execution Details](https://learn.microsoft.com/en-us/rest/api/power-bi/datasets/get-refresh-execution-details-in-group)) | **200 with Viewer only**, for a completed refresh (02:24:43Z), a Direct Lake framing refresh (02:24:53Z) and a **failed** one (02:26:31Z, with the engine `messages` and `serviceExceptionJson`, i.e. the error text). It needs the refresh's request id, and there is no way to list ids as a Viewer. |

No dataset or workspace grant was made or changed by this investigation. The permission the documentation names for the list is dataset
**Write**; this was not tested, because testing it means granting a write role. The semantic model permissions article says Write is
implicit for workspace Admin, Member and Contributor and never for Viewer, and the users API documentation says it "can't be used to
grant dataset Write" ([semantic model permissions](https://learn.microsoft.com/en-us/power-bi/connect-data/service-datasets-permissions),
[Post Dataset User In Group](https://learn.microsoft.com/en-us/rest/api/power-bi/datasets/post-dataset-user-in-group)). Read from the
documentation, that leaves the Contributor workspace role for a service principal, which also lets it create and change every item in
the workspace.

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

## 12. Storage identities, encryption and network

| Resource | Who can do what | Notes |
|---|---|---|
| PostgreSQL Flexible Server | Authentication is Microsoft Entra only (`passwordAuth` disabled). The Entra administrator **group** (parameter) can administer and run `scripts/postgres-bootstrap.sql`; the **reader managed identity** is a database role that owns one schema and may connect, with no superuser, no `CREATE` on the database and nothing in other schemas | The actor (write) identity has no access. The server is reachable over public access with explicit firewall rules (or VNet integration with a private DNS zone); TLS is required and the app verifies the certificate chain and host name |
| Evidence storage account | Shared-key access disabled, Entra only. The reader identity has **Storage Blob Data Contributor on the `evidence` and `audit-anchors` containers only** and **Storage Blob Data Reader on the `contracts` container only**, never a role on the account. Whoever uploads the contracts file needs Contributor on the `contracts` container alone | Versioning, soft delete and account-level version-level immutability (retention parameter; `Unlocked` until the owner chooses `Locked`, which is irreversible). Blob public access off, TLS 1.2+, no cross-tenant replication |
| Container App | Has no volume and no storage key. The estate contracts file is read from the `contracts` blob (read-only for the app) | Rate limiting and in-memory caches are per replica |

Encryption at rest is the platform default for both services (Microsoft-managed keys); in transit is TLS. The audit
anchors make the audit trail verifiable even against someone with full rights on the database, but not against someone
who can change the immutability policy of the storage account before it is locked: keep the account's own RBAC small and
lock the policy in production. Credentials for these services never appear in the repository, in settings or in
CI: the only secrets in the picture are Container App secrets that Bicep fills from Key Vault references.

