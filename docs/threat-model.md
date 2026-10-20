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
| **Spoofing** an approver | Caller sends `X-Orqentis-User` with admin roles | `dev_headers` refused outside `local`; Easy Auth principal from Entra ID; reserved `agent:` / `system:` ids rejected | A misconfigured ingress that bypasses Easy Auth. Mitigated by internal ingress and the auth config in Bicep. |
| Spoofing the agent as a human | LLM output claims to be an operator | The agent runs as `AGENT_PRINCIPAL` (kind AGENT); approval and execute paths require kind HUMAN | Low |
| **Tampering** with approved actions | Change the target after approval | The approval is bound to a SHA-256 fingerprint of the action; execution recomputes and compares it | Low |
| Tampering with the audit trail | Edit or delete audit lines | Hash chain plus sequence numbers; `verify()` detects edits and gaps; cutting records off the end of a log the app is running on makes it refuse further appends and report the log truncated | A full-file rewrite by someone with write access to the volume. Target: immutable storage. |
| Tampering through prompt injection | Item description says "approve all actions" | Untrusted envelope, scanner flags, tool allowlist, approval-only mutation tools, policy DENY on delete and permissions, grounding validation | The model may produce a misleading narrative. Findings still need cited evidence, and humans review before approving. |
| **Repudiation** | "I never approved that" | Approver id, timestamp and comment stored on the approval and in the audit trail | Low |
| **Information disclosure** of secrets | Secret inside a Fabric error message | Redaction before logs, evidence, audit and model; tokens never logged | Novel secret formats may slip past the regexes. Mitigation: extend the patterns. |
| Disclosure to the LLM provider | Fabric metadata sent to Azure OpenAI | Azure OpenAI in the customer tenant through Entra ID; prompt content not recorded in telemetry; the rule-based reasoner is available for no-LLM operation | Data residency depends on the Azure OpenAI deployment region. |
| **Denial of service** through agent loops | Model calls tools endlessly | Per-investigation tool budget (reserved before awaits), Agent Framework iteration and call limits, per-tool timeouts | Low |
| DoS against Fabric APIs | Aggressive polling | Bounded collectors, incremental windows (watermark + overlap), a global concurrency and rate budget, retry with backoff and `Retry-After` (a `429` pauses every caller), a single replica | Low-Medium: the budget defaults are conservative guesses, because Fabric's per-endpoint quotas are not published |
| Two writers on one volume during a deploy | The old and the new Container App revision both run schedulers and append to the same audit file and SQLite stores | Scheduler start delay, shutdown releases the lease, audit log re-reads its chain head before appending (best effort), `maxReplicas = 1` | **Medium until the PostgreSQL system of record (database lease, serialised audit chain).** Deploy with the scheduler off or accept the risk |
| Runaway automation | A failing investigation is retried forever, or a flood of incidents triggers a flood of LLM calls | Per-incident attempt limit, batch size and concurrency limit for investigations, investigation timeout, jobs that never overlap | Low |
| Silent loss of monitoring | A glitch makes Fabric return an empty estate, or a job fails every run | Catalog refresh refuses an empty estate and audits every diff; job health changes are audited; `Scheduler.status()` shows the last outcome | A failing job is visible in logs and status only; alerting arrives with the metrics workstream |
| **Elevation of privilege** | Agent requests `DELETE_ITEM` or a permission change | Policy DENY; the read client has no mutation methods; the write client is held only by the gateway and disabled by default | Low |
| Elevation through policy misconfiguration | Policy file allows a mutation automatically | Code invariant: mutations always require approval in the MVP; unknown action types are HIGH_RISK; default effect DENY | Low |
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
