# ADR-0018: Notifications and the web UI: implementation choices

* Status: Accepted
* Date: 2026-10-20
* Implements [ADR-0010](0010-human-interface.md) (programme decision D3). Builds on [ADR-0015](0015-principal-classification-and-api-hardening.md): it closes the cookie-session CSRF requirement recorded there (ledger R6).

## Context

ADR-0010 decided a `Notifier` port with a Teams Workflows webhook and a generic webhook, and a minimal server-rendered UI behind Easy Auth. This ADR records how they are built and why, because both add a new attack surface: the application now makes outbound calls to a URL it is configured with (request forgery), and it now serves HTML to browsers that carry a session cookie which classifies as a person (cross-site request forgery, script injection from Fabric metadata).

## Decision

### 1. Notifications are published, not called

The domain services publish a `NotificationEvent` to a `NotificationSink` (`orqentis/notifications/sink.py`). `publish` is synchronous, constant time and never raises. Four places publish, always **after what they announce is stored** (and, where there is one, its audit record written):

| Event | Where | Event id (the de-duplication key) |
|---|---|---|
| incident opened | `IncidentService.create` | `incident-opened:<incident>` |
| incident escalated (severity raised, today by an SLA breach found in the investigation) | `IncidentService.raise_severity` (async: timeline entry, **save**, then publish), called by the pipeline-failure workflow in place of its former inline assignment (the timeline text is unchanged) | `incident-escalated:<incident>:<severity>` |
| approval requested | `ApprovalService.request` | `approval-requested:<approval>` |
| action executed / failed | `ActionGateway.execute`, also when the write outcome is unknown (reported as a failure to reconcile) | `action-executed:<approval>` / `action-failed:<approval>` |

"Stored" means the authoritative record: the incident for an opening or an escalation, the approval for a request, and for an execution the approval's claim and finished outcome, which are written before people are told that an action ran; the incident's timeline entry for the execution is bookkeeping that follows and is not waited for. A test records writes and publications in one log and checks that order, and that a failing save announces nothing.

Nothing about policy, approval or execution changes: the sink is a parameter that defaults to a no-op, and the hooks sit after the existing logic. With `ORQENTIS_NOTIFIER=null` (the default, and what `make demo` uses) the sink discards events and no notification record is ever written.

### 2. A bounded in-memory outbox with a single worker

`NotificationDispatcher` cleans the event into a `NotificationMessage`, drops it if its event id was seen (bounded set), appends it to a bounded `deque` and wakes one worker task, started and stopped with the scheduler by `Runtime`. The worker retries a retryable failure (timeout, network error, 5xx, 408/425/429) with exponential back-off, "equal jitter" and the destination's `Retry-After`, up to `ORQENTIS_NOTIFY_MAX_ATTEMPTS`; a permanent failure (a refused destination, a redirect, any other 4xx) is not retried. When the outbox is full the **oldest** message is dropped: recent news is worth more than a stale backlog behind a dead webhook. Every attempt (`SENT`, `RETRYING`, `FAILED`), every drop and every message abandoned at shutdown is a hash-chained audit record (`notification.send`) with the event id, notifier, destination **host**, attempt and outcome. A failing, slow or hanging webhook therefore costs the callers nothing: tests run detection, an investigation, an approval and an execution against a notifier that always fails, hangs, or raises.

*Limitation, deliberate:* the outbox is in memory, so a notification not yet delivered when the process dies is lost (it is audited as `ABANDONED` on a clean stop only). A durable outbox needs the durable system of record (ADR-0008, WS6); doing it on SQLite now would be thrown away. The incident, the approval and the UI are the source of truth, and the notification is the nudge.

### 3. What leaves the system

Every string of a message passes through `orqentis.notifications.content`: control characters and envelope tags removed (`sanitize_text`), secrets redacted (the same `redact_text` as logs, evidence and audit), whitespace collapsed, and a length limit per field (headline 200, fact value 300, 12 facts). Fabric metadata is untrusted (invariant 8) and a chat card is where people look hardest, so the Teams adapter additionally defuses Markdown: `[` `]` become `(` `)` and `<` `>` look-alike quotes, so an item called `[Re-enter your password](https://evil.example)` shows as text and can never become a link. The Teams card has **no actions at all** (no `Action.Submit`, `Action.Execute` or `Action.OpenUrl`): one Markdown link built from the configured public base URL and an id. Teams approvals stay deferred (ADR-0010).

Deep links go to `/ui/incidents/<id>` and `/ui/approvals/<id>` under `ORQENTIS_PUBLIC_BASE_URL`. With interactive sign-in configured the link goes through Easy Auth (`/.auth/login/aad?post_login_redirect_uri=<path>`), so a person without a session is sent to Entra ID and lands on the page.

### 4. SSRF guard (`orqentis/notifications/ssrf.py`, `urlcheck.py`)

A webhook URL is configuration, but it is also the one place the application calls a host it does not control, from inside the Container Apps environment. Per call: https only, a DNS name (no IP literal), no credentials, a host on the allow list (default for Teams: the Power Platform / Logic Apps hosts; empty means any public host for the generic webhook); then the name is resolved **by the application** and **every** address must be public (not private, loopback, link-local, multicast, reserved, CGNAT, the Azure host endpoint `168.63.129.16` and the metadata addresses, or an IPv6 range that embeds an IPv4 address); one bad answer refuses the call. The request is then sent **to a validated address** with the real name as `Host` and TLS server name, so there is no second resolution between the check and the connection (the DNS rebinding window) and the certificate is still verified against the real host. Redirects are never followed (a 3xx is a failure), environment proxies are ignored, and one deadline covers resolution and request. The same URL and allow-list rules run when the settings load, so a bad URL stops the start with a message that never contains the URL (the Teams signature is in its query string). `Settings` sets `hide_input_in_errors`, because otherwise a failing validation prints `input_value`, a truncated copy of the raw environment, and a start-up traceback would carry the tail of a signing key or webhook signature; a test breaks the settings in several ways, in several environment orders, and checks the message, the repr and the traceback for every secret and fragments of it. The HTTP client's loggers are pinned to WARNING (it logs every request URL at INFO and re-encodes characters, so a string filter alone cannot be relied on) and a filter drops any record that still carries the secret path. Also refused as non-public: IPv6 site-local `fec0::/10` and `192.88.99.0/24`, and the other deprecated or documentation prefixes.

### 5. Webhook URLs and keys are Key Vault references

`notifyTeamsWebhookSecretUri`, `notifyWebhookSecretUri`, `notifyWebhookSigningSecretUri`, `uiCsrfSecretUri` and `uiSignInClientSecretUri` are Key Vault secret URIs (no version). Container Apps resolves them with the reader identity into secrets that reach the app as `secretRef` environment variables; the reader's `Key Vault Secrets User` role is assigned on those secrets only, through the existing per-secret module. There is no Bicep parameter that takes a URL, and the existing guard test rejects a parameter file that does. The deployment fails with a readable reason (the existing input-check pattern) for a malformed or foreign URI or a notifier without its secret.

### 6. The UI: server-rendered, no script, the API's guards

Jinja2 templates under `/ui`, packaged with the application, autoescape on everywhere and no `|safe`. There is no JavaScript, no inline style and no external resource, and the CSP says so: `default-src 'none'; style-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'`, with `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin`, `Cache-Control: no-store` (pages hold incident data and form tokens) and, over https, HSTS. Escaping is the control; the CSP is the second line.

Every route depends on the same guards as the API route that does the same thing (`ReadPrincipal`, `VerifyPrincipal` for audit verification, `DecidingPrincipal` for approve / reject / execute) and calls the same service methods (`ApprovalService.decide`, `ActionGateway.execute`). The UI adds no decision logic and no privilege: it offers a button when it could work, and a click that should not work is refused by the services (requester is not approver, role, expiry, fingerprint, single use, policy). Errors are rendered as HTML pages by wrapping the existing exception handlers, so the API's status codes and codes are unchanged. The structural route test now also walks routers outside `orqentis.api.routers` (newer FastAPI wraps an included router in one object, so the old walk would never have seen the UI's routes).

### 7. CSRF: two independent layers (ledger R6)

1. **Cross-origin guard** (`orqentis/ui/security.py`), for every `POST/PUT/PATCH/DELETE`, API and UI, before authentication and before the body is read: `Sec-Fetch-Site` must be `same-origin` or `none` when present; `Origin` must be this application's origin (the `Host` or `X-Forwarded-Host` it arrived with, using `X-Forwarded-Proto` for the scheme, or `ORQENTIS_PUBLIC_BASE_URL`) when present, `Origin: null` included; on `/ui` one of `Origin`/`Referer` must be present and match. On the API a request with no browser headers is a script or `curl` and passes: a browser always sends `Origin` on a cross-site POST. The same guard also covers the **reads that spend a rate budget** (`GET /ui/*` and `GET /audit/verify`): a page on another site could otherwise load `<img src="/ui/audit">` with the person's cookie and use up their shared "expensive" budget, so that their own `POST /incidents/{id}/investigate` answers 429. A request marked `cross-site` or `same-site` is refused unless it is a top-level navigation (`Sec-Fetch-Mode: navigate`, `Sec-Fetch-Dest: document`), which is what following a link in a card, or the redirect back from Entra ID, is. The API also stays **JSON-only**: a body that is not `application/json` is 415. Before this, `POST /approvals/{id}/approve` without a body was a valid CSRF target for any ID-token session.
2. **Form token** on every state-changing UI form: `v1.<expiry>.<nonce>.<HMAC-SHA256(key, principal id, expiry, nonce)>`, stateless, bound to the signed-in principal's object id, valid four hours (the approval TTL), different on every rendering. The key is `ORQENTIS_UI_CSRF_SECRET` (a Key Vault secret) or, without it, a random per-process key, which is correct for one replica; with more replicas it must be shared or a form rendered by one replica is refused by another (documented, and a follow-up when WS6 allows replicas).

Token and Origin failures are refused with 403 and, for the token (the person is known by then), audited with the bounded refusal sampling that already protects the audit log.

### 8. Interactive sign-in uses a Key Vault client secret, not a federated credential

ADR-0010 asked to prefer a managed-identity federated credential over a secret where Container Apps supports it. Checked against current Microsoft documentation on 2026-10-20: the App Service article documents the pattern as a preview feature (a federated identity credential on the app registration, an app setting `OVERRIDE_USE_MI_FIC_ASSERTION_CLIENTID` and `clientSecretSettingName` pointing at it), but the Container Apps article for the Microsoft Entra provider (updated 2026-02-20) offers only a client secret, and `microsoft/azure-container-apps` issue #1693 (opened 2026-04-13, still open, labelled `enhancement`) reports that the Container Apps sidecar does not recognise the sentinel and treats the managed identity's client id as a literal secret (`AADSTS7000215`), with an unconfirmed report that a lower-case secret name works. That is neither documented nor supported, so it is not used. The template sets `clientSecretSettingName` to a Container App secret that is a **Key Vault reference** (`uiSignInClientSecretUri`); the secret exists only in Key Vault, never in a parameter, output, pipeline variable or the repository. It enables the authorization code flow instead of the implicit flow Microsoft discourages. The template keeps `Return401` (an anonymous API call stays a 401, the CD smoke test depends on it) and turns the token store off (the app needs the ID token's claims, not an access or refresh token); an 8 hour session; no external redirect. When Container Apps supports the federated credential, replace the secret: the registration then needs no secret to rotate or expire.

## Consequences

* Notifications can be switched on per environment with two Key Vault references and an origin; with nothing configured the behaviour, the audit trail and `make demo` are what they were.
* People can read incidents and decide approvals from a browser, with the API's checks, and can be sent there by a card.
* The ADR-0015 CSRF requirement is met for the API as well as the UI, at the cost of one rule: a browser-originated state-changing request must be same-origin JSON (API) or a same-origin form with a token (UI).
* The client secret of the UI registration expires (Entra allows at most 24 months) and has to be rotated in Key Vault; WS9 should alert on it.
* A person who opens `/ui` without a session gets Easy Auth's `401`, not a login page, because the template keeps `Return401`. The sign-in link in the notification, and `https://<app>/.auth/login/aad?post_login_redirect_uri=/ui`, are the entry points.
* **Residual risks:** the app still trusts `X-MS-CLIENT-PRINCIPAL` (ADR-0015); the outbox is lost on a crash; a per-process CSRF key does not work across replicas; Teams delivery success (HTTP 202) means the flow accepted the request, not that a card appeared (check the flow's run history).

## Alternatives considered

* **Call the notifier inline from the services.** Rejected: a slow webhook would slow detection and every approval.
* **A durable outbox in SQLite now.** Rejected: WS6 replaces that storage; the audit trail records failures meanwhile.
* **Resolve with the normal HTTP client and check the address first.** Rejected: that leaves the rebinding window between the check and the connection.
* **Teams cards with approve / reject buttons.** Deferred by ADR-0010.
* **`SameSite` cookie alone for CSRF.** The cookie belongs to Easy Auth, whose attributes this application does not control or document; the two layers above do not depend on them.
* **A single-page application or a template engine with client scripts.** Rejected: no script means no script injection, and nothing to review beyond the templates.
* **`RedirectToLoginPage` for unauthenticated requests.** Rejected: it would turn every anonymous API call into a redirect and break the 401 contract and the smoke test.

## Follow-ups

* Durable outbox with the PostgreSQL storage (WS6); a replica-shared CSRF key when more than one replica runs.
* Federated credential for the sign-in as soon as Container Apps documents it; alert on the client secret's expiry (WS9).
* Fan-out to more than one notifier; Teams Adaptive Card approvals (still deferred, ADR-0010).
* In-app token validation (ADR-0015 follow-up), which would also make the UI independent of the front end.
