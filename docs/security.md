# Security

FabricOps can influence production data platforms, so it is designed around one rule: **the agent
can look, but it cannot touch.** Every change must pass a deterministic policy engine and an
explicit human approval bound to the exact action. On top of that, the deployment must have its
write capability turned on.

## 1. Identities and capability boundaries

| Identity | Used by | Permissions | Notes |
|---|---|---|---|
| Reader (operating identity), e.g. user-assigned managed identity `id-orqentis-reader` | `RestFabricClient` (all reads); Agent Framework chat client | Fabric workspace **Viewer** on monitored workspaces (includes semantic model read for refresh history); Azure OpenAI **Cognitive Services OpenAI User**; AcrPull on the image registry | Least privilege. No write role anywhere. |
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

### 2.2 Roles and what they unlock

| Role | Read endpoints | `POST /telemetry/collect` | `POST /incidents/{id}/investigate` | Approve / reject / execute |
|---|---|---|---|---|
| `FabricOps.Reader` | yes | no | no | no |
| `FabricOps.Automation` | yes | yes | no | never |
| `FabricOps.Operator` | yes | yes | yes | LOW/MEDIUM risk, human only |
| `FabricOps.PlatformAdmin` | yes | yes | yes | HIGH_RISK, human only |

Under Easy Auth a caller needs one of these roles for every endpoint except `/health`
(`ORQENTIS_API_REQUIRE_READ_ROLE`, on by default under `easy_auth`). A principal with no role gets 403.

### 2.3 Request limits

* Bodies over `ORQENTIS_API_MAX_BODY_BYTES` (64 KiB by default) get **413**, both when `Content-Length`
  declares too much and when a chunked body grows past the limit.
* Mutating calls (`ORQENTIS_API_RATE_LIMIT_MUTATING_PER_MINUTE`, 30) and expensive ones, telemetry
  collection, investigations and audit verification (`ORQENTIS_API_RATE_LIMIT_EXPENSIVE_PER_MINUTE`, 6),
  are limited per principal and answer **429** with `Retry-After`. Reads are not limited by the app.
* Limits live in memory per replica (the MVP runs one). A global limit, WAF and edge logging belong
  in Front Door or API Management.

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
* Rate limits are per replica and in memory, and plain reads are not limited (see 2.3). The default ingress is
  external (authenticated); use `allowedIpRanges` or `vnet` where the API must not be internet-reachable.
* Easy Auth's claim-name mapping and the `sub == oid` behaviour of app tokens are handled from documentation
  and synthetic principals. They have not been checked against a live tenant (ADR-0015).
* No REST write adapter is shipped. Enabling writes against a real tenant first requires
  implementing and reviewing one.
