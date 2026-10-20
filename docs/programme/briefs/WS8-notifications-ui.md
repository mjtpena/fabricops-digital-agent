You are implementing **WS8: Notifications and web UI** in `mjtpena/fabricops-digital-agent`.
Base: latest `origin/main`. Branch: `prod/ws8-notifications-ui`. Open one PR to `main` when done; never push to `main`.

## Context

Decision D3 / [ADR-0010](../../adr/0010-human-interface.md): a `Notifier` port with a Microsoft Teams **Workflows
webhook** adapter and a **generic webhook** adapter; a minimal **server-rendered** UI behind Easy Auth; Teams Adaptive
Card approvals deferred. Today humans learn nothing unless they poll the API, and approvals need raw API calls.

Wave 1 delivered: autonomous scheduling and async investigations (ADR-0013), principal classification where HUMAN means
"a user token" and app-only callers are SERVICE (ADR-0015; ID-token browser sessions are HUMAN; app roles
`FabricOps.Operator`, `FabricOps.PlatformAdmin`, `FabricOps.Reader`, `FabricOps.Automation`), route-level authz guards
(`api/authz.py`) and a test that every non-public route carries one, atomic approval transitions, and the ledger
requirement R6: **cookie sessions need CSRF protection** (JSON-only or token-protected state-changing routes plus an
Origin check).

## Goal

A human is told within a minute when an incident opens or escalates, when an approval is requested, and when an action
executes, with a deep link; and can read the incident report and approve, reject or execute from a browser, with
exactly the same policy, role and approval checks as the API.

## In scope

1. **`Notifier` port** and adapters:
   * Teams Workflows webhook (Power Automate "When a Teams webhook request is received"), posting an Adaptive Card
     **without** action buttons (links only);
   * generic webhook (JSON, optional HMAC-SHA256 signature header with a secret from Key Vault);
   * a null notifier for local/mock and tests.
   Retries with backoff and jitter, a timeout, de-duplication (one notification per event id), a bounded outbox so a
   failing webhook never blocks detection or investigation, and audit of every send attempt and outcome.
2. **What is sent** (all content passes the existing redaction; Fabric metadata is untrusted and must be sanitised and
   length-limited before it reaches a card): incident opened, incident escalated (severity up or SLA breached), approval
   requested (action, target, risk, required role, expiry), action executed or failed. Each with a deep link built from a
   configured public base URL.
3. **SSRF and secrets.** Webhook URLs are secrets (Container App secret with a Key Vault reference; never in settings
   files, logs or audit). Allow only `https`; refuse private, loopback, link-local and metadata addresses after DNS
   resolution; optional host allowlist (default for Teams: the Power Platform / Logic Apps webhook hosts). No redirects.
4. **Server-rendered UI** (Jinja2 templates, no SPA, minimal CSS, no external CDNs) mounted under `/ui`:
   incident list (filter by status/severity), incident detail and the report with FACT / INFERENCE / RECOMMENDATION
   labels and evidence ids, approvals list and detail with approve / reject / execute and a comment, audit verification.
   Every route uses the existing authz guards; approval routes use the same service calls as the API (no duplicated
   decision logic). Escape all untrusted text; set a strict Content-Security-Policy, `X-Frame-Options: DENY`,
   `Referrer-Policy`, `X-Content-Type-Options`.
5. **CSRF (ledger R6).** State-changing UI routes require a per-session CSRF token (double-submit or signed token) AND a
   same-origin `Origin`/`Referer` check; API routes stay JSON-only. Tests for each refusal.
6. **Easy Auth for browsers.** Configure interactive sign-in (`/.auth/login/aad`) in Bicep. Find out from current
   Microsoft documentation whether Container Apps authentication supports a **managed identity federated credential**
   instead of a client secret for the Entra provider; use it if supported, otherwise a Key Vault-referenced client secret,
   and document the choice. Keep `/health*` anonymous and everything else authenticated.
7. Settings, `.env.example`, Bicep parameters (notifier kind, Key Vault secret references, public base URL, allowlist),
   docs (`docs/architecture.md`, `docs/security.md`, `infra/bicep/README.md`, a short "Set up Teams notifications" guide),
   ADR for the UI and notifier implementation choices, and the threat-model rows (SSRF, CSRF, XSS via Fabric metadata,
   notification content disclosure).

## Out of scope

Teams Adaptive Card approvals (deferred). Any live tenant or Teams access (the orchestrator does the live test). Storage
changes (WS6), metrics/alerts (WS9). New detection (WS7).

## Acceptance criteria

1. Tests: notifier retries with backoff and stops at the limit; a failing webhook never blocks detection or an
   investigation; de-duplication by event id; every send is audited; redaction removes secrets from card content.
2. Tests: SSRF guard refuses http, private/loopback/link-local/metadata addresses (including via DNS rebinding to a
   private IP), and redirects; allowlist enforced.
3. UI route tests with role enforcement: a role-less user, a Reader, an Operator, a PlatformAdmin and an app-only
   principal each get exactly the allowed pages and actions; the requester cannot approve their own request; an app-only
   principal can never approve or execute through the UI.
4. CSRF tests: missing token, wrong token, foreign Origin each refused; XSS test: a Fabric item description containing
   script markup renders inert.
5. `make check`, `make demo` (offline, null notifier), `pytest -m security`, `docker build`, `az bicep build` and
   `build-params` pass.
6. Live (done by the orchestrator after merge): a notification arrives in Teams with a working deep link.
