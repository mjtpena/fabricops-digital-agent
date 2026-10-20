# ADR-0015: Principal classification from token claims, Automation role and API hardening

* Status: Accepted
* Date: 2026-10-20
* Supersedes the Easy Auth handling in ADR-0006 (every Easy Auth principal was HUMAN). Approval and execution rules are unchanged.

## Context

In deployed environments Container Apps authentication validates the Entra ID token and forwards the
caller as `X-MS-CLIENT-PRINCIPAL`. `orqentis.api.auth` marked **every** such principal `HUMAN`, app-only
(client-credentials) tokens included. Approval and execution require a `HUMAN` principal holding a role, so an
application that had been granted an approver role could approve and execute. That is a latent privilege
escalation, and the registration script offered no role for automation identities anyway. Other gaps: the
default ingress (`externalIngress = false`) left the API unreachable for people, and there was no request size
limit or rate limiting. A server-rendered UI behind Easy Auth (WS8) will add human callers who arrive with
**ID-token sessions** (cookie), not only with bearer access tokens, so both must stay `HUMAN`.

## Decision

### 1. Classify callers from the claims, failing closed

`orqentis.api.claims.classify` turns the claims into a `PrincipalKind`. `HUMAN` needs **positive evidence of a
user token and no app-only or contradictory signal**. Everything else is `SERVICE`. A set of claims that is
ambiguous or contradictory is additionally flagged `ambiguous`: the API refuses it with `403
PRINCIPAL_AMBIGUOUS` instead of serving it, and it is never `HUMAN`.

Signals, checked against the Microsoft identity platform documentation (access-token claims reference, optional
claims reference, "Secure applications and APIs by validating claims"):

| Signal | What the documentation says | How it is used |
|---|---|---|
| `idtyp = app` | Optional claim on access tokens; "the most accurate way for an API to determine if a token is an app token". Emitted for app-only tokens by default once the API requests it; user tokens only get it with `include_user_token`. **Absent unless the API's registration requests it.** | Positive app signal. Absence proves nothing. Other values (`device`, unknown) are unsupported, so ambiguous. `user` is a positive user signal. |
| `scp` | "Only included for user tokens." The validating-claims article tells APIs to expect `scp` to be absent for **daemon (app-only) tokens and for ID tokens**. | Positive user signal. Its absence is **not** an app signal on its own, because ID-token browser sessions look the same (the `roles`-without-`scp` heuristic would turn WS8's human sessions into services). |
| `sub` vs `oid` | `sub` is "a pairwise identifier unique to a particular application ID"; `oid` is the object id of the user or service principal. In app tokens both carry the service principal's object id. The documentation states the pairwise property for users; `sub == oid` for app tokens is established behaviour but is not spelled out in the documentation. | `sub == oid` is a positive app signal. A user token must show `sub` present and different from `oid`. That requirement is a **necessary** condition for `HUMAN`, never a sufficient one. |
| Role `FabricOps.Automation` | The role only allows `Application` members (`allowedMemberTypes`). | Positive app signal. |
| `preferred_username`, `upn`, `email`, `unique_name`, `name` | Mutable display attributes; "never use" them for authorization. | Used only as *presence* evidence for the no-`scp` case, never as an identity or for access decisions. App tokens carry none. |

The identity of the caller is always the `oid`. A principal without an `oid`, or with different values for
`oid`, `sub` or `idtyp`, is rejected (401, respectively ambiguous). Claim names are accepted in both the JWT
spelling and the legacy WS-Federation URIs Easy Auth uses (`.../objectidentifier`, `.../nameidentifier`,
`.../scope`, ...).

#### Decision table

Rules are evaluated in order; the first match decides. `ambiguous` rows are refused with 403. Every row is a
test case (`tests/unit/test_principal_classification.py`).

| # | Conditions | Kind | Rule | Ambiguous |
|---|---|---|---|---|
| 1 | `oid`, `sub` or `idtyp` has more than one different value | SERVICE | `CONFLICTING_CLAIMS` | yes |
| 2 | no `oid` | SERVICE | `NO_OBJECT_ID` (the API answers 401) | yes |
| 3 | `idtyp` present and not `app`/`user` | SERVICE | `UNSUPPORTED_IDTYP` | yes |
| 4 | app signal (`idtyp=app`, `sub==oid`, role Automation) **and** user signal (`scp`, `idtyp=user`) | SERVICE | `CONTRADICTORY_SIGNALS` | yes |
| 5 | any app signal | SERVICE | `APP_ONLY` | no |
| 6 | no `sub` | SERVICE | `NO_SUBJECT` | yes |
| 7 | `scp` present, `sub != oid` | HUMAN | `DELEGATED_ACCESS_TOKEN` | no |
| 8 | no `scp`, `sub != oid`, and `idtyp=user` or a user identity claim | HUMAN | `USER_TOKEN_NO_SCOPE` (ID-token session) | no |
| 9 | anything else (no `scp`, `sub != oid`, no user identity claim) | SERVICE | `NO_USER_IDENTITY` | yes |

Consequences worth stating: a token with **only** roles and no other claims (the `roles`-without-`scp`
signal on its own) is row 6 and is not `HUMAN`; an `idtyp=app` token with no `sub` is row 5; a token with
`sub == oid` and nothing else is row 5; an ordinary delegated user token is row 7; a browser session's ID token
(no `scp`, `preferred_username`/`name`, pairwise `sub`) is row 8. A test sweeps every combination of
{`oid`, `sub`, `scp`, `idtyp`, roles, identity claims} and asserts that `HUMAN` is never produced when an app
signal, a conflict or a missing pairwise subject is present.

#### Defence in depth in the routes

* `DecidingPrincipal` (approve, reject, execute) requires `kind == HUMAN` **and** that the token does not
  carry `FabricOps.Automation`, independent of every setting. A refusal is audited (`api.authorize`,
  `REFUSED`). The approval service and the action gateway keep their own checks, which remain the source of
  truth (invariant 6).
* A test walks every router and fails if a route does not authenticate, if a mutating route has no role
  guard, or if approve/reject/execute lose the human-only guard.

### 2. App roles

| Role | Member types | Purpose |
|---|---|---|
| `FabricOps.Operator`, `FabricOps.PlatformAdmin` | User | Unchanged: investigate, approve (LOW/MEDIUM; HIGH_RISK respectively). |
| `FabricOps.Automation` | Application | Schedulers, smoke tests: read endpoints and `POST /telemetry/collect`. Never approve, reject, execute, or investigate. |
| `FabricOps.Reader` | User, Application | Read endpoints only. |

`Automation` and `Reader` are API roles. They are not in the policy's `role_hierarchy`, which ranks approval
roles only.

**Do read endpoints need a role?** Yes, by default under Easy Auth (`ORQENTIS_API_REQUIRE_READ_ROLE`, default:
on for `easy_auth`, off for `dev_headers`). The switch governs plain reads only. Collection, investigation and audit
verification **always** need their roles under Easy Auth, so turning reads open cannot let a role-less caller
open approval requests or trigger Fabric reads. Before, any authenticated caller could read, and the only gate was
`appRoleAssignmentRequired=true` on the enterprise application, which the template cannot enforce. A caller
with no FabricOps role (misconfigured assignment, or a default-access assignment) would have read incidents,
evidence and the audit trail. Requiring a role makes the deployment safe even if that setting is wrong.
`/health` stays public. The interactive docs and `/openapi.json` are not served under Easy Auth: FastAPI serves
them without a dependency, so they would answer any authenticated caller, role or not. Collection needs Automation, Operator or PlatformAdmin; investigations need Operator
or PlatformAdmin (they run the LLM and open approvals); Reader reads only. With enforcement off, behaviour is
as before (any authenticated caller), which local development and the demo rely on.

### 3. Request limits

`orqentis.api.middleware` (one `install_api_hardening(app, settings)` call in `create_app`):

* **Size.** `ORQENTIS_API_MAX_BODY_BYTES` (default 64 KiB; the largest legitimate body is a short approval
  comment). A declared `Content-Length` over the limit is refused with 413 before the app runs. Chunked or
  under-declared bodies are counted while read and stop with 413 (an `HTTPException` subclass, because FastAPI
  rewrites any other exception raised while reading the body to 400). An invalid `Content-Length` is 400.
* **Rate.** Token bucket per *authenticated* principal and request class, in memory (bounded, see section 5). `mutating` (approve, reject,
  execute; default 30 per minute) and `expensive` (collection, investigation, audit verification; default 6 per
  minute). Beyond the budget: 429 with `Retry-After` (whole seconds until a token is available). Plain reads
  are not limited. The budget is charged by the route guards (`orqentis.api.authz`) after `current_principal`
  and before the role check, keyed on the principal id from the validated claims. It is **not** a middleware
  keyed on the raw header: a first version did that and a forged `X-MS-CLIENT-PRINCIPAL` sent to `/health`
  (which Easy Auth does not authenticate) could drain a named approver's budget. Refused attempts are charged
  too, so a caller without the right role cannot flood the audit log. Requests without a valid identity are
  rejected by Easy Auth before the app and are not limited by it.
* **Per replica.** State is in memory, so the effective limit is N times the setting with N replicas. The MVP
  runs one replica (SQLite stores). A global limit belongs at the edge (Front Door, API Management).

### 4. Ingress modes in Bicep

`ingressMode` replaces the boolean: `externalAuthenticated` (default), `vnet`, `internal`. Authentication is on
in every mode. `allowedIpRanges` adds an address allow list (`ipSecurityRestrictions`) and
`allowedClientApplications` an Easy Auth client-application allow list (`defaultAuthorizationPolicy`; empty =
unrestricted, recommended in production). The deprecated `externalIngress` still works when `ingressMode` is not
set, so existing parameter files behave exactly as before. Anything that would silently change the exposure
fails the deployment instead: setting both parameters, `vnet` without a subnet, `externalAuthenticated` with one.
The mode matrix was evaluated offline against the compiled template.

**Default: external and authenticated.** The old default made the API unreachable for people. Exposure is
defence in depth here, while the access control is the validated Entra token plus assignment-required plus the
role and human-only checks. The cost is that a public address exists and answers 401. The default changes for
deployments that never set `externalIngress`; the README states it and `allowedIpRanges`/`vnet` tighten it.

**Health endpoints.** `/health` and `/health/ready` are excluded from authentication by default
(`anonymousHealthEndpoints`) so uptime checks and smoke tests work without a token. They return the version,
mode (`fabric_mode`, `reasoner`) and the time. The write-capability flag is not disclosed anonymously (`GET /system/info`). Container Apps does not say whether `excludedPaths` matches exactly or by prefix; the
app does not depend on it: all other routes need the identity header, and nothing under `/health` reads it.
Set the parameter to `false` where version disclosure matters more than anonymous probes.

### 5. Hardening of the approval boundary and the request path (review round 2)

* **Decisions and execution are atomic.** `ApprovalProvider.transition` is a compare-and-set on a revision
  counter kept on every approval (SQLite: a conditional `UPDATE`; WS6's PostgreSQL adapter implements the same
  port with a row lock or `WHERE revision = $n`). Approve, reject, expire and cancel go through it, so of two
  concurrent decisions, even from two processes, exactly one is stored and the other gets 409. A per-approval
  lock in the service spares same-process callers the conflict. Reproduced before the fix: five concurrent
  executes made five write calls, and a concurrent approve and reject both returned 200.
* **Single use is claimed before the write.** `execute` persists `EXECUTING` with a compare-and-set before the write
  client is called and finalises `SUCCEEDED`/`FAILED` afterwards. If the write raises anything but a `FabricError`
  the outcome is unknown, the claim stays `EXECUTING` and an `UNKNOWN` audit record is written: a retry must not
  repeat a write that may have happened, so a person reconciles it. (At-most-once; the write client still
  receives the approval id as its idempotency key.)
* **Execution re-evaluates the current policy.** Besides a DENY, the current decision's risk and required role
  are enforced: an approval granted at one risk is not executed after the action was reclassified higher
  (`POLICY_RISK_ESCALATED`), and the executor must hold the role the current policy requires **and** the role
  recorded with the approval (the stricter of the two). Reproduced before the fix: after the policy was tightened
  to PlatformAdmin an Operator still executed.
* **Deciding routes check the role before the approval is looked up** (`require_decider`, behind the kind-only
  `require_human`). A caller without an approver role gets the same 403 whether the approval exists or not, and
  cannot probe approvals. Collection and investigation roles are on whenever `action_roles_required` holds
  (always under Easy Auth).
* **Refusals are audited with a bound.** Every refusal is logged. The hash-chained audit trail gets one record per
  principal and reason per minute and at most 20 per minute overall, with a count of the skipped ones in the
  next record, so an authenticated caller cannot fill the log with refusals.
* **Exactly one `X-MS-CLIENT-PRINCIPAL` header** is accepted (two or more: 401), and the same for the dev headers.
  A forged principal placed before the genuine one used to win. The header is decoded with strict base64
  (no comma-joined or whitespace-padded pairs), is capped at 32 KiB, and a parse failure of any kind, including a
  deeply nested document (`RecursionError`), is a 401, never a 500.
* **The payload cannot relabel claims.** `role_typ` and `name_typ` are honoured only for the claim types Easy Auth
  uses (`roles` and the ws/2008 role URI; `name`, `preferred_username`, `upn`, `email` and the xmlsoap name URI).
  `role_typ = "idtyp"` used to fold `idtyp = app` into the roles and made the principal `HUMAN`.
* **Rate-limit tables are bounded without a bypass.** When the table is full only buckets that have refilled are
  evicted, and otherwise new principals share one overflow bucket; a flood of new keys can no longer reset a
  bucket that is limiting someone.
* **`dev_headers` fails closed.** `orqentis serve` refuses it on a non-loopback address unless
  `ORQENTIS_DEV_HEADERS_ALLOW_REMOTE=true`, and the container image defaults to `ORQENTIS_API_AUTH_MODE=easy_auth`
  (every route but `/health` answers 401), so `docker run` without configuration is not an open API. `make serve`
  and `make demo` are unchanged (127.0.0.1). For a throwaway local container: `-e ORQENTIS_API_AUTH_MODE=dev_headers
  -e ORQENTIS_DEV_HEADERS_ALLOW_REMOTE=true`, published on loopback only.
* **`/health` is liveness only.** The write-capability flag moved to `GET /system/info`, which needs a FabricOps
  role. The anonymous answer is `status`, `version`, `fabric_mode`, `reasoner` and `time`.
* **The route-guard test walks the live app**: routes placed directly on the app, the OpenAPI surface and the
  doc routes are all inspected, and a kind-only guard no longer counts as a role guard.

### 6. What `HUMAN` means, and what it does not

`HUMAN` means **the token is a user token**, not that a person is at the keyboard. These also classify `HUMAN`:
service accounts that sign in with a password (ROPC), Entra "agent users", and third-party client applications that
act for a user who consented to them. The classifier cannot see the difference, and `scp`/`sub` do not carry it.
Mitigations that exist: `allowedClientApplications` (Easy Auth admits only listed client applications),
assignment-required with user/group assignment of the approver roles (only assigned users can hold them), and the
human-only, requester-is-not-approver and role checks in the service. Requiring an `amr` such as MFA for deciders is a
possible follow-up. Record: do not assign approver roles to non-interactive accounts.

**CSRF is a WS8 requirement, not a current risk** (the API has no login flow configured, so browsers never
send a cookie to it). When the UI adds cookie sessions, an ID-token session is `HUMAN` (row 8), so a foreign page
could make a signed-in browser POST `/approvals/{id}/approve`. Before WS8 ships: state-changing routes accept
`application/json` only (a form post gets 415), check `Origin`/`Sec-Fetch-Site` against the app's own origin,
and keep `SameSite` cookies. No code is needed in this ADR's scope.

**Residual risk: trust-the-header.** The app trusts `X-MS-CLIENT-PRINCIPAL` and never validates a bearer token.
With an externally reachable endpoint, any window without active Easy Auth (the first deployment before the
child `authConfigs` resource applies, authentication disabled by mistake, a platform fault) lets a forged header
name a `FabricOps.PlatformAdmin`. This PR mitigates it with a documented safe first deployment (internal first,
verify, then expose), per-deployment checks that tokenless and forged-header requests get 401, and the optional
IP and client-application allow lists. It does not remove it. **Follow-up:** validate the token in the app
(signature, issuer, audience, `tid`) or require Easy Auth's `X-MS-TOKEN-AAD-*` token store and verify it, so
the app no longer depends on the front end alone.

## Consequences

* An app-only token can no longer become `HUMAN`, whatever roles it carries; applications get a role that
  allows automation and nothing more.
* **Breaking for callers under Easy Auth.** Principals with only `oid` and roles (no `sub`) used to be
  `HUMAN` and are now refused. Real Entra tokens always carry `sub`. The Easy Auth claim-name mapping
  (`sub` arrives as `.../nameidentifier`) is handled but has only been checked against documentation and
  synthetic principals, not a live tenant. If a deployment's principals do not carry a `sub`, people are
  *denied*, never promoted, and `403 PRINCIPAL_AMBIGUOUS` with the logged rule identifies the cause.
* The app registration must request the `idtyp` optional claim and v2 access tokens (README step 1).
  Without `idtyp` the other signals still classify app tokens.
* Callers need a FabricOps role to read under Easy Auth. Existing assignments (Operator, PlatformAdmin) keep
  working; assign `FabricOps.Reader` to read-only people.
* WS8 can use browser sessions as they are (row 8). It will add the client secret or federated credential and
  the redirect handling on the auth config; none of that is needed or present now.

## Alternatives considered

* **`roles` without `scp` means app-only.** Rejected: ID tokens never carry `scp`, so every browser session
  would become a service and WS8 could not approve.
* **Rely on `idtyp` alone.** Rejected: the claim exists only if the registration requests it; a missing
  claim cannot be told apart from a user token.
* **Strip roles from ambiguous principals instead of refusing them.** Rejected: a principal we cannot
  classify should not be served at all, and a clear 403 with a code is easier to diagnose.
* **A separate allow list of automation object ids in settings.** Rejected: the Entra role assignment is the
  existing, auditable place for that decision.
* **Enforce the read role also under `dev_headers`.** Rejected: every demo, doc example and test uses
  role-less viewers locally. The switch exists for those who want it.

## Follow-ups

* In-app bearer-token validation (see the residual risk above).
* Make `allowedClientApplications` mandatory in production once the client list is known.
* Two-person approval for HIGH_RISK (WS11) builds on the human-only guard.
* Move rate limiting to a shared store when the app scales beyond one replica (WS6).
