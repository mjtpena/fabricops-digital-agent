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
| Tampering with the audit trail | Edit or delete audit lines | Hash chain plus sequence numbers; `verify()` detects edits and gaps; cutting records off the end of a log the app is running on makes it refuse further appends and report the log truncated | A full-file rewrite by someone with write access to the volume. Target: immutable storage. |
| Tampering through prompt injection | Item description says "approve all actions" | Untrusted envelope, scanner flags, tool allowlist, approval-only mutation tools, policy DENY on delete and permissions, grounding validation | The model may produce a misleading narrative. Findings still need cited evidence, and humans review before approving. |
| Double execution / lost rejection under concurrency | Concurrent `/execute` calls reach the write client several times; an approve and a reject race and the rejection is lost | Decisions are compare-and-set transitions (revision counter); the execution claim (`EXECUTING`) is persisted before the write call; per-approval lock in process; unknown write outcomes keep the claim | Low. A crash between claim and finalise leaves `EXECUTING` (at-most-once); a person reconciles it |
| Execution under a stale policy | The policy is tightened or the action reclassified after approval, and the old approval is executed | The current policy is re-evaluated at execution: deny, risk escalation (`POLICY_RISK_ESCALATED`) and the required role (the stricter of current and recorded) | Low |
| Probing approvals without a role | A caller with a valid token and no approver role tells existing from missing approvals by 404 vs 403 | Role is checked before the approval is looked up; uniform 403; refusals are audited with a bounded sample | Low |
| Open API from an unconfigured container | `docker run` of the image with no configuration lets any caller choose its roles (dev headers) | The image defaults to `easy_auth`; `orqentis serve` refuses `dev_headers` on a non-loopback address without an explicit opt-in | Low; the opt-in is documented for loopback test containers only |
| Browser-driven approvals (CSRF) | With WS8 cookie sessions a foreign page makes a signed-in browser POST approve | Not applicable today (no login flow configured). WS8 requirement: JSON-only state-changing routes, `Origin`/`Sec-Fetch-Site` check, SameSite cookies | Must be built with WS8 (ADR-0015) |
| **Repudiation** | "I never approved that" | Approver id, timestamp and comment stored on the approval and in the audit trail | Low |
| **Information disclosure** of secrets | Secret inside a Fabric error message | Redaction before logs, evidence, audit and model; tokens never logged | Novel secret formats may slip past the regexes. Mitigation: extend the patterns. |
| Disclosure to the LLM provider | Fabric metadata sent to Azure OpenAI | Azure OpenAI in the customer tenant through Entra ID; prompt content not recorded in telemetry; the rule-based reasoner is available for no-LLM operation | Data residency depends on the Azure OpenAI deployment region. |
| **Denial of service** through agent loops | Model calls tools endlessly | Per-investigation tool budget (reserved before awaits), Agent Framework iteration and call limits, per-tool timeouts | Low |
| DoS against Fabric APIs | Aggressive polling | Bounded collectors, incremental windows (watermark + overlap), a global concurrency and rate budget, retry with backoff and `Retry-After` (a `429` pauses every caller), a single replica | Low-Medium: the budget defaults are conservative guesses, because Fabric's per-endpoint quotas are not published |
| DoS against the API | Oversized bodies, floods of approvals or expensive calls (collection, investigation, audit verification) | 413 over `ORQENTIS_API_MAX_BODY_BYTES`; per-principal token buckets answer 429 with `Retry-After`, charged **after** authentication and keyed on the validated principal id (a forged header on `/health` or an unrouted path cannot spend anyone's budget); refused attempts are charged too; Easy Auth rejects tokenless traffic before the app | Limits are per replica and reads are not limited. A distributed flood needs edge protection (Front Door/WAF, `allowedIpRanges`). An authenticated insider can exhaust only their own budget. |
| Two writers on one volume during a deploy | The old and the new Container App revision both run schedulers and append to the same audit file and SQLite stores | Scheduler start delay, shutdown releases the lease, audit log re-reads its chain head before appending (best effort), `maxReplicas = 1` | **Medium until the PostgreSQL system of record (database lease, serialised audit chain).** Deploy with the scheduler off or accept the risk |
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
