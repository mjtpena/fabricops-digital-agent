You are implementing **WS3: Identity and API hardening** in `mjtpena/fabricops-digital-agent`.
Base: latest `origin/main`. Branch: `prod/ws3-identity-hardening`. Open one PR to `main` when done; never push to `main`.

## Context

In deployed environments the API authenticates callers through Azure Container Apps built-in
authentication (Easy Auth), which validates the Entra ID token and forwards the caller as the
`X-MS-CLIENT-PRINCIPAL` header (base64 JSON with `claims`, `role_typ`, `name_typ`, `auth_typ`).
`src/orqentis/api/auth.py` (`_from_easy_auth`) turns that into a `Principal`, and it marks **every**
Easy Auth principal as `PrincipalKind.HUMAN`, including app-only (client-credentials) tokens from
service principals and managed identities. Approval and execution require a HUMAN principal with a
role (`src/orqentis/approvals/service.py`, `src/orqentis/actions/gateway.py`), so today an
application granted an app role could approve and execute. That is a latent privilege escalation.

Also: the app roles in the registration script (`infra/bicep/README.md` §1) allow only `User`
members, so no automation identity can call the API (schedulers, smoke tests). The default
`externalIngress = false` makes the API unreachable for people. There is no request size limit and
no rate limiting. There is no UI; a later workstream (WS8) adds a server-rendered UI behind Easy
Auth with browser sign-in, so human principals will also arrive from **ID-token sessions** (cookie),
not only from bearer access tokens.

## Goal

Principals are classified correctly and fail closed: app-only callers are `SERVICE`, never `HUMAN`,
and can never approve or execute. Automation identities get a narrow app role. The API is reachable
by people by default, safely, and resists oversized and abusive requests.

## In scope

1. **Principal classification from token claims.** Classify Easy Auth principals as `SERVICE` when
   the token is app-only. Use positive, documented signals and fail closed when they are missing or
   contradictory. Signals to evaluate (verify each against Microsoft identity platform docs):
   * `idtyp = app` (an optional claim for access tokens; document how to enable it in the app
     registration's token configuration);
   * an access token with `roles` but no `scp` claim;
   * `sub == oid` (app-only tokens use the service principal's object id as subject; user tokens
     use a pairwise `sub`).
   Human principals must keep working both for delegated access tokens (`scp` present) and for
   ID-token browser sessions (needed by WS8). Write the decision table into the ADR and test every
   row, including ambiguous inputs, which must not yield `HUMAN`.
2. **`FabricOps.Automation` app role** (`allowedMemberTypes: ["Application"]`): may call read
   endpoints and `POST /telemetry/collect`; may never approve, reject or execute (the HUMAN check
   already blocks it; add an explicit route-level check as defence in depth, plus tests).
   Optionally add `FabricOps.Reader` for read endpoints. Decide whether read endpoints need a role
   at all and record the decision (today any authenticated caller can read).
3. **Request limits.** Maximum request body size (reject with 413), and basic per-principal rate
   limiting on mutating and expensive endpoints (429 with `Retry-After`). In-memory per replica is
   acceptable; document that limits are per replica. Put this in a separate module (for example
   `src/orqentis/api/middleware.py`) with a single registration line in `create_app`.
4. **Ingress modes in Bicep.** Replace the boolean with a documented mode parameter (keep backwards
   compatibility for `externalIngress` or document the migration): internal; external with Easy
   Auth (Return401; optional IP allowlist via `ipSecurityRestrictions`); VNet-internal. Choose a
   safe default that people can reach, which is **external, authenticated**, unless you find a
   reason not to; record the reasoning. Decide whether `/health` (and WS1's `/health/ready`) are
   excluded from Easy Auth (`excludedPaths`) so uptime checks and smoke tests can reach them; they
   expose only version and mode flags.
5. **App-registration script** in `infra/bicep/README.md`: add the Automation role (and Reader if
   added), the `idtyp` optional claim, `requestedAccessTokenVersion = 2`, and how to assign an app
   role to a managed identity or service principal (`az rest` to
   `/servicePrincipals/{id}/appRoleAssignedTo`). No secrets.
6. Update `docs/security.md` and `docs/threat-model.md` (spoofing, elevation rows), the policy
   file's role documentation if relevant, and add an ADR for principal classification.

## Out of scope

The UI itself (WS8). Two-person approval (WS11). Storage (WS6). CI/CD (WS4). Any live tenant
access.

## Coordination with parallel workstreams

* WS1 owns the lifespan and health routes in `api/app.py`; you add one middleware registration
  line there.
* WS2 adds Bicep parameters for time zone and service-principal mode; you change the ingress and
  auth-config blocks. Keep changes additive and merge `origin/main` before the PR.

## Likely files

`src/orqentis/api/auth.py`, `src/orqentis/api/middleware.py` (new), `src/orqentis/api/deps.py`,
`src/orqentis/api/routers/*.py` (route-level role checks), `src/orqentis/domain/principals.py`
(only if needed), `src/orqentis/policies/default_policy.yaml` (role list only, if needed),
`infra/bicep/README.md`, `docs/security.md`, `docs/threat-model.md`, tests. Hot files you may touch
additively: `settings.py`, `.env.example`, `infra/bicep/main.bicep`, `README.md`.

## Acceptance criteria

1. Security tests (marked `security`): an app-only principal (each signal on its own, and
   combined) holding **any** role, including `FabricOps.PlatformAdmin`, gets **403** on approve,
   reject and execute, and is classified `SERVICE`.
2. Tests: delegated access tokens and ID-token sessions for users are `HUMAN` with their roles;
   ambiguous claim sets are never `HUMAN`.
3. Tests: an Automation principal can `GET /incidents` and `POST /telemetry/collect`, and cannot
   approve or execute.
4. Tests: oversized bodies get 413; exceeding the rate limit gets 429 with `Retry-After`; limits
   are configurable.
5. `pytest -m security` passes, with the new tests included.
6. `az bicep build` / `build-params` pass for each ingress mode; README documents the modes and the
   updated app-registration script.
7. `make check`, `make demo`, `docker build` pass.

Note for the PR: this PR gets an extra adversarial review by an independent reviewer looking for
ways to obtain a HUMAN principal from an app-only token or to reach approve/execute without one.
