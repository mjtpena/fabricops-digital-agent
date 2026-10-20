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
on for `easy_auth`, off for `dev_headers`). Before, any authenticated caller could read, and the only gate was
`appRoleAssignmentRequired=true` on the enterprise application, which the template cannot enforce. A caller
with no FabricOps role (misconfigured assignment, or a default-access assignment) would have read incidents,
evidence and the audit trail. Requiring a role makes the deployment safe even if that setting is wrong.
`/health` stays public. Collection needs Automation, Operator or PlatformAdmin; investigations need Operator
or PlatformAdmin (they run the LLM and open approvals); Reader reads only. With enforcement off, behaviour is
as before (any authenticated caller), which local development and the demo rely on.

### 3. Request limits

`orqentis.api.middleware` (one `install_api_hardening(app, settings)` call in `create_app`):

* **Size.** `ORQENTIS_API_MAX_BODY_BYTES` (default 64 KiB; the largest legitimate body is a short approval
  comment). A declared `Content-Length` over the limit is refused with 413 before the app runs. Chunked or
  under-declared bodies are counted while read and stop with 413 (an `HTTPException` subclass, because FastAPI
  rewrites any other exception raised while reading the body to 400). An invalid `Content-Length` is 400.
* **Rate.** Token bucket per principal and request class, in memory. `mutating` (any non-safe method, default
  30 per minute) and `expensive` (collection, investigation, audit verification, default 6 per minute). Beyond
  the budget: 429 with `Retry-After` (whole seconds until a token is available). Plain reads are not limited.
  The key is the object id from the validated principal (or `X-Orqentis-User` under `dev_headers`), not any
  other header, so a caller cannot dodge its budget by varying headers. Callers without an identity share a
  bucket per client address, so they cannot starve authenticated ones.
* **Per replica.** State is in memory, so the effective limit is N times the setting with N replicas. The MVP
  runs one replica (SQLite stores). A global limit belongs at the edge (Front Door, API Management).

### 4. Ingress modes in Bicep

`ingressMode` replaces the boolean: `externalAuthenticated` (default), `vnet`, `internal`. Authentication is on
in every mode. `allowedIpRanges` adds an address allow list (`ipSecurityRestrictions`). The deprecated
`externalIngress` still works and overrides the mode, so existing parameter files behave exactly as before.
A mode that contradicts `infrastructureSubnetId` fails the deployment instead of silently changing the exposure.

**Default: external and authenticated.** The old default made the API unreachable for people. Exposure is
defence in depth here, while the access control is the validated Entra token plus assignment-required plus the
role and human-only checks. The cost is that a public address exists and answers 401. The default changes for
deployments that never set `externalIngress`; the README states it and `allowedIpRanges`/`vnet` tighten it.

**Health endpoints.** `/health` and `/health/ready` are excluded from authentication by default
(`anonymousHealthEndpoints`) so uptime checks and smoke tests work without a token. They return the version,
mode flags and the time. Container Apps does not say whether `excludedPaths` matches exactly or by prefix; the
app does not depend on it: all other routes need the identity header, and nothing under `/health` reads it.
Set the parameter to `false` where version disclosure matters more than anonymous probes.

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

* Restrict allowed client applications in Easy Auth (`defaultAuthorizationPolicy.allowedApplications`) for
  `Automation` callers.
* Two-person approval for HIGH_RISK (WS11) builds on the human-only guard.
* Move rate limiting to a shared store when the app scales beyond one replica (WS6).
