# Threat model

Scope: the Orqentis FabricOps MVP (API, collectors, investigation workflow, investigator agent,
policy and approval, local stores), deployed as a single Container App with a managed identity.

## Assets

| Asset | Why it matters |
|---|---|
| Fabric estate (data, pipelines, models, permissions) | Production data platform. Unintended change means data loss or outage. |
| Reader / actor identities | Token theft would give read access (reader) or change capability (actor). |
| Incident records, evidence, reports | May contain sensitive metadata, and they are an operational record. |
| Audit trail | Accountability for every decision and attempted change. |
| Approval decisions | The control that gates every mutation. |
| LLM context | Can be steered by injected content. |

## Trust boundaries

1. Fabric / Power BI APIs → collectors and tools. **All returned data is untrusted.**
2. Investigator agent (LLM) ↔ tool registry. **The model is untrusted.** Its outputs are proposals.
3. API clients → FastAPI, through Entra ID (Easy Auth).
4. Policy and approval → action gateway → write client.
5. Application → local stores and audit log.

## STRIDE

| Threat | Example | Mitigations | Residual risk |
|---|---|---|---|
| **Spoofing** an approver | Caller sends `X-Orqentis-User` with admin roles | `dev_headers` refused outside `local`; Easy Auth principal from Entra ID; reserved `agent:` / `system:` ids rejected | A misconfigured ingress that bypasses Easy Auth. Mitigated by the auth config in Bicep (`Return401` on every path but health), the optional IP allow list and the VNet mode. |
| Spoofing with duplicated identity headers | Request carries a forged `X-MS-CLIENT-PRINCIPAL` in front of (or comma-joined with) the genuine one | Exactly one header is accepted (several: 401); strict base64 rejects comma/whitespace-joined values; size cap; the payload cannot relabel claims as roles; parse failures of any kind (including deep nesting) are 401 | Depends on Easy Auth stripping client copies, which has not been verified on a live tenant |
| Spoofing a person with an application token | A service principal or managed identity holding (or assigned by mistake) an approver role calls approve or execute | App-only tokens are classified `SERVICE` from `idtyp`, `sub == oid` or the Automation role and never `HUMAN`; ambiguous or contradictory claims are refused (`PRINCIPAL_AMBIGUOUS`); human-only check at the route, the approval service and the gateway; approver roles are User-only app roles; tests sweep the claim combinations | An Entra or Easy Auth change that alters claims such that app tokens look like users. Not testable offline; the classification needs a pairwise `sub`, an extra user-identity claim and no app signal. Verify with a captured principal after changes (infra README). |
| Trust-the-header with Easy Auth missing | The externally reachable app runs without Container Apps authentication (first deployment before the auth config applies, authentication disabled by mistake, platform fault); a forged `X-MS-CLIENT-PRINCIPAL` names a PlatformAdmin | Safe first-deployment procedure (internal first, verify authentication, then expose; infra README); per-deployment checks that tokenless and forged-header requests get 401; RBAC and alerting on auth-config changes; `allowedIpRanges` / `vnet`; `allowedClientApplications` | **High if it happens.** The app does not validate bearer tokens itself. In-app token validation (signature, issuer, audience) is a recorded follow-up (ADR-0015). |
| Forged identity header on an unauthenticated path | Caller sends `X-MS-CLIENT-PRINCIPAL` on `/health`, which Easy Auth does not authenticate, or on an unrouted path | No route under `/health` reads the header and nothing derives an identity from it before authentication (rate limits are charged after `current_principal`); the exclusion lists exact paths; every other route needs the header and Easy Auth strips client copies on authenticated paths | Container Apps does not document whether `excludedPaths` matches by prefix. Set `anonymousHealthEndpoints = false` to remove the exclusion. |
| Spoofing the agent as a human | LLM output claims to be an operator | The agent runs as `AGENT_PRINCIPAL` (kind AGENT); approval and execute paths require kind HUMAN | Low |
| **Tampering** with approved actions | Change the target after approval | The approval is bound to a SHA-256 fingerprint of the action; execution recomputes and compares it | Low |
| Tampering with the audit trail | Edit or delete audit lines | Hash chain plus sequence numbers; `verify()` detects edits and gaps; cutting records off the end of a log the app is running on makes it refuse further appends and report the log truncated | With the PostgreSQL backend: a rewrite of the whole table with every hash recomputed is caught by the immutable audit anchor (ADR-0017); records newer than the latest anchor (up to one anchoring interval) are protected by the chain alone. The local JSONL log (development) is not anchored. |
| Tampering through prompt injection | Item description says "approve all actions" | Untrusted envelope, scanner flags, tool allowlist, approval-only mutation tools, policy DENY on delete and permissions, grounding validation | The model may produce a misleading narrative. Findings still need cited evidence, and humans review before approving. |
| Double execution / lost rejection under concurrency | Concurrent `/execute` calls reach the write client several times; an approve and a reject race and the rejection is lost | Decisions are compare-and-set transitions (revision counter); the execution claim (`EXECUTING`) is persisted before the write call; per-approval lock in process; unknown write outcomes keep the claim | Low. A crash between claim and finalise leaves `EXECUTING` (at-most-once); a person reconciles it |
| Execution under a stale policy | The policy is tightened or the action reclassified after approval, and the old approval is executed | The current policy is re-evaluated at execution: deny, risk escalation (`POLICY_RISK_ESCALATED`) and the required role (the stricter of current and recorded) | Low |
| Probing approvals without a role | A caller with a valid token and no approver role tells existing from missing approvals by 404 vs 403 | Role is checked before the approval is looked up; uniform 403; refusals are audited with a bounded sample | Low |
| Open API from an unconfigured container | `docker run` of the image with no configuration lets any caller choose its roles (dev headers) | The image defaults to `easy_auth`; `orqentis serve` refuses `dev_headers` on a non-loopback address without an explicit opt-in | Low; the opt-in is documented for loopback test containers only |
| Browser-driven approvals (CSRF) | A foreign page makes a signed-in browser (Easy Auth cookie session = a person) POST `/approvals/{id}/approve` or `/ui/approvals/{id}/approve`, even with no body | Cross-origin guard on every state-changing request (`Sec-Fetch-Site`, `Origin`, `Referer` on `/ui`; `null` refused); the API accepts JSON bodies only (415); each UI form carries an HMAC token bound to the signed-in principal, expiring, different per page; refusals audited; reads that spend a budget (`/ui/*`, `/audit/verify`) refuse cross-site subresource loads (Fetch Metadata), so a foreign page cannot drain a person's rate budget; tests for a missing, wrong, other-person's, expired and tampered token and for foreign, `null` and missing origins | A browser that sends neither `Origin` nor `Sec-Fetch-Site` on a cross-site `POST` (none of the supported ones); a per-process CSRF key does not work across replicas until `ORQENTIS_UI_CSRF_SECRET` is shared (ADR-0018) |
| Request forgery through the notifier (SSRF) | A webhook URL, or a DNS name behind it, points at `169.254.169.254`, `168.63.129.16`, another Container App or `localhost`; DNS rebinding; a redirect to an internal address | https only, DNS name (no IP literal), host allow list, application-side resolution with every address required to be public, connection pinned to the validated address with the real name for Host and TLS, redirects never followed, environment proxies ignored, one deadline; the same URL rules run when the settings load; tests for each address class, mixed answers, numeric spellings, rebinding and redirects | The allow list defaults to the Power Platform hosts; widening it (`notifyAllowedHosts`) is the operator's decision. The destination host sees the app's egress address |
| Script injection through Fabric metadata (XSS) | A pipeline description or name holds `<script>` or an `onerror` handler and an approver opens the incident or approval page | Jinja2 autoescape everywhere, no `\|safe`, no JavaScript, no inline script or style; strict CSP (`default-src 'none'`), `X-Frame-Options: DENY`, `nosniff`; error pages escape too; tests render hostile values in every untrusted field and check the templates statically | A browser or template-engine bug. The CSP is the second line, not the first |
| Link and Markdown injection in notifications | An item named `[Re-enter your password](https://evil.example)` becomes a link in a Teams card people trust | Brackets and angle brackets defused in card text; the card has no actions and one link built from the configured base URL and an id; control characters, tags and secrets removed; length limits | Plain URLs in text may still be auto-linked by Teams; they show as the URL |
| Disclosure through notifications | Incident titles, item names, severities, action targets and requesters sent to a channel or endpoint that is too broad, or an audit/log line holding the webhook URL | Redaction before send; no URL, only the host, in logs, audit and errors; the URL is a Key Vault secret with a per-secret role; the content is limited to what the UI shows to a reader; guidance to use a private channel | Anyone in the channel sees the facts. Nothing in a card is a credential, but names can be sensitive |
| Notification flooding or a dead webhook | A failure storm floods Teams (4 requests / second limit) or a hung endpoint blocks the app | De-duplication by event id, one worker, bounded outbox with drop-oldest and an audit record, retries with back-off and `Retry-After`, a timeout covering DNS and request; publishing never blocks or raises, tested with a notifier that fails, hangs or crashes | Notifications waiting when the process dies are lost; the UI and the audit trail still have everything |
| **Repudiation** | "I never approved that" | Approver id, timestamp and comment stored on the approval and in the audit trail | Low |
| **Information disclosure** of secrets | Secret inside a Fabric error message | Redaction before logs, evidence, audit and model; tokens never logged | Novel secret formats may slip past the regexes. Mitigation: extend the patterns. |
| Disclosure to the LLM provider | Fabric metadata sent to Azure OpenAI | Azure OpenAI in the customer tenant through Entra ID; prompt content not recorded in telemetry; the rule-based reasoner is available for no-LLM operation | Data residency depends on the Azure OpenAI deployment region. |
| **Denial of service** through agent loops | Model calls tools endlessly | Per-investigation tool budget (reserved before awaits), Agent Framework iteration and call limits, per-tool timeouts | Low |
| DoS against Fabric APIs | Aggressive polling | Bounded collectors, incremental windows (watermark + overlap), a global concurrency and rate budget, retry with backoff and `Retry-After` (a `429` pauses every caller), rate limits per replica | Low-Medium: the budget defaults are conservative guesses, because Fabric's per-endpoint quotas are not published |
| DoS against the API | Oversized bodies, floods of approvals or expensive calls (collection, investigation, audit verification) | 413 over `ORQENTIS_API_MAX_BODY_BYTES`; per-principal token buckets answer 429 with `Retry-After`, charged **after** authentication and keyed on the validated principal id (a forged header on `/health` or an unrouted path cannot spend anyone's budget); refused attempts are charged too; Easy Auth rejects tokenless traffic before the app | Limits are per replica and reads are not limited. A distributed flood needs edge protection (Front Door/WAF, `allowedIpRanges`). An authenticated insider can exhaust only their own budget. |
| Several writers (two replicas, overlapping revisions during a deploy) | Two instances append to the audit chain, decide or execute the same approval, run schedulers, or write after losing the lease | Database-serialised audit chain (head row lock), compare-and-set approvals (one execution), database lease with fencing on every scheduled write, detection mutex, durable jobs claimed with `SKIP LOCKED`; two-instance, two-process and kill-a-process tests | Low. Residual: the incident aggregate is last-writer-wins (a row version is the follow-up) |
| Runaway automation | A failing investigation is retried forever, or a flood of incidents triggers a flood of LLM calls | Per-incident attempt limit, batch size and concurrency limit for investigations, investigation timeout, jobs that never overlap | Low |
| Silent loss of monitoring | A glitch makes Fabric return an empty estate, or a job fails every run | Catalog refresh refuses an empty estate and audits every diff; job health changes are audited; `Scheduler.status()` shows the last outcome | A failing job is visible in logs and status only; alerting arrives with the metrics workstream |
| **Elevation of privilege** | Agent requests `DELETE_ITEM` or a permission change | Policy DENY; the read client has no mutation methods; the write client is held only by the gateway and disabled by default | Low |
| Elevation by a low-privilege automation identity | The scheduler identity tries to approve, execute or investigate | `FabricOps.Automation` (Application members only) allows reads and collection only; route-level refusal of any token carrying it; SERVICE kind blocked in service and gateway | Low |
| Elevation through policy misconfiguration | Policy file allows a mutation automatically | Code invariant: mutations always require approval in the MVP; unknown action types are HIGH_RISK; default effect DENY | Low |
| `HUMAN` is a user token, not a person | A password-signing service account, an agent user, or a third-party app acting for a consenting user approves | `allowedClientApplications`; approver roles assigned only to interactive people via assignment-required; requester-is-not-approver and role checks; audit | Medium until MFA (`amr`) can be required for deciders. Do not assign approver roles to non-interactive accounts |
| Confused deputy | Approver approves an action they don't understand | Report labels FACT / INFERENCE / RECOMMENDATION, cites evidence ids, shows risk and policy rules, and binds the approval to an exact target | Human error remains possible. Two-person approval for HIGH_RISK is on the roadmap. |

## Abuse cases covered by tests

* An agent calls a mutation tool and gets `ACTION_REQUIRES_APPROVAL`; nothing executes.
* An agent executes a pending or approved approval and is blocked (`PRINCIPAL_NOT_HUMAN`).
* A human executes without approval and is blocked (`ACTION_REQUIRES_APPROVAL`).
* A tampered action is blocked (`ACTION_FINGERPRINT_MISMATCH`).
* A model obeys an injected instruction, requests DELETE and approves: DENIED, and the unknown
  tool is not found.
* Fabricated evidence ids or runbooks are rejected by grounding.
* Expired, double-decided and self-approved approvals are refused.
* Audit tampering and deletion are detected.
* A secret in a Fabric error never reaches evidence or the report.
* A job or build failure with a secret in its message is redacted in logs, status and audit, and the unauthenticated readiness probe shows no exception message at all (fixed code and exception type only).
* An automatic investigation requests approvals but cannot approve or execute them.
* An app-only principal holding any role, `FabricOps.PlatformAdmin` included, gets 403 on approve, reject and execute,
  for each app-only signal alone and combined, and nothing reaches the write client.
* Ambiguous or contradictory claims (an `idtyp=app` token with `scp`, unknown `idtyp`, no `sub`, roles only) are
  never HUMAN; users with delegated tokens and ID-token sessions are.
* An Automation identity can read and collect, and cannot investigate, approve, reject or execute.
* Oversized bodies get 413 and exceeding a rate limit gets 429 with `Retry-After`; a forged principal header on `/health`
  or an unrouted path cannot drain a named approver's budget.
* Turning off the read-role switch under Easy Auth does not open collection or investigation, and the interactive docs
  are not served to role-less callers.
* Through the web UI, a role-less user gets nothing, a reader and an operator get exactly the pages and buttons their roles allow, an application
  token holding every role can neither approve nor execute, and the requester cannot approve their own request.
* A decision POST with no token, a wrong token, another person's token, an expired or tampered token, a foreign, `null` or missing `Origin`, or
  a cross-site `Sec-Fetch-Site` is refused and changes nothing; so is a cross-origin or form-encoded POST to the API.
* A Fabric item description containing script markup renders as inert text in every page.
* A webhook to `http://`, to a private, loopback, link-local or metadata address (also through DNS rebinding, numeric host spellings and
  redirects) or to a host off the allow list is refused; a failing, slow or hanging webhook never blocks detection or an investigation.
