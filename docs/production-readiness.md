# Production readiness ledger

The single record of the programme that takes Orqentis FabricOps from a verified PoC to
production (`v1.0.0`). It is updated after every merge, live action and decision. Evidence is a
link to a PR, a CI run, a log or a document, never a claim.

Status values: `not started`, `in progress`, `blocked`, `done`.

Last updated: 2026-10-20 (Wave 2 started).

## 1. Definition of done

| # | Criterion | Status | Evidence |
|---|---|---|---|
| 1 | **Continuous delivery.** `dev`, `test`, `prod` deployed by GitHub Actions CD over OIDC; no secrets in repo or pipeline; manual approval for `prod`; `main` CI green | in progress | CD pipeline merged (WS4 #6: build once, Trivy, SBOM, promote by digest, required reviewers, OIDC only). Build identity configured with the immutable OIDC subject. Pending: deploy identities, GitHub environments and rulesets (Wave 2 list B3/B4), first dev deploy |
| 2 | **Autonomous operation.** `rest` mode against the live tenant; scheduled collect, detect, investigate, notify without human prompting | in progress | Scheduler, auto-investigation and async jobs merged (WS1 #4). Pending: rest mode in a deployed environment, notifications (WS8) |
| 3 | **Detection on real failures.** Schema drift, notebook failure, pipeline timeout, credential/auth failure, semantic-model refresh failure, SLA breach: each detected within 10 min, investigated automatically, correct category (or honest LOW-confidence "undetermined"), human notified with a report link | in progress | Live sandbox and 6 injectors (WS5 #7): schema drift, notebook, timeout and auth failures open incidents; refresh failure and SLA breach need WS7 rules (and D9) |
| 4 | **Stateless and resilient.** 2+ replicas, leader election, zero-downtime deploys, audit chain verifies across replicas and deploys; no Azure Files or SQLite in deployed environments | in progress | Lease port, durable detection cursor, compare-and-set approvals merged (WS1, WS3). Pending: PostgreSQL adapters, 2+ replicas (WS6) |
| 5 | **Durable data.** Backups or PITR; one restore rehearsed and documented | not started | |
| 6 | **Operable.** Alerts for app down, restart loop, 5xx rate, collector errors, collection heartbeat (15 min), audit verification failure; test fire done; `docs/operations.md` | not started | |
| 7 | **Secure.** Threat model updated; independent review with every finding fixed or accepted in writing; no unaccepted high/critical in dependency and image scans; SBOM published; app-only tokens can never approve or execute | in progress | App-only tokens can never approve or execute (WS3 #3, independent red-team, 218 security tests). Image: Azure Linux 3, Trivy 0 HIGH/CRITICAL; pip-audit gate; SBOM generated in CD. Pending: WS12 review, published SBOM on release |
| 8 | **Portable.** Fresh RG and different tenant from docs + parameter file + contracts scaffold; declared environment classification; configurable time zone; sovereign-cloud scopes/authority (unit-tested) | in progress | Declared environments, contracts schema/validate/scaffold, time zone and sovereign-cloud settings (unit-tested), cross-tenant service principal with Key Vault (WS2 #5). Pending: portability rehearsal (Wave 4) |
| 9 | **Supervised remediation.** `RERUN_PIPELINE` and `REFRESH_SEMANTIC_MODEL` end to end in sandbox DEV via approval flow and a separate executor holding only the actor identity; writes off by default and off in `prod`; two approvers for HIGH_RISK; "no mutation is ever auto-allowed" unchanged | not started | |
| 10 | **LLM reasoner validated.** Golden-set eval against real Azure OpenAI; accuracy ≥ rule-based; 0 ungrounded findings accepted; cost and latency recorded; fallback proven | not started | |
| 11 | **Soak.** 72 h continuous in `test` (or read-only `prod`), no unhandled errors, no unexplained restarts | not started | |
| 12 | **Quality gates.** `make check` passes; `make demo` offline; ADR per architecture change; `v1.0.0` tagged with CHANGELOG | in progress | `main` CI green on every merge (ruff, format, mypy --strict, pytest incl. 218 security tests, runbooks, Bicep, Docker); `make demo` offline; ADRs 0008–0016 |

## 2. Workstreams

At most four sub-sessions run at once. Each workstream is one branch (`prod/<ws-id>-<slug>`) and
one PR to `main`. Briefs live in [`docs/programme/briefs/`](programme/briefs/).

| WS | Title | Wave | Depends on | Status | Owner session | PR | Evidence |
|---|---|---|---|---|---|---|---|
| WS0 | CI fix (Docker Hub 429; Windows local gates) | 0 | | merged | orchestrator | [#1](https://github.com/mjtpena/fabricops-digital-agent/pull/1) | Local gates green; CI not started (B1) |
| WS1 | Autonomous runtime | 1 | | merged | `ws1-runtime` (Sonnet) | [#4](https://github.com/mjtpena/fabricops-digital-agent/pull/4) | fcefe0d: review 1 (11 findings) + verification round (audit truncation refused, port contract corrected, late symptoms attached) all fixed; orchestrator re-ran gates: make check, security, demo-all green |
| WS2 | Portability and contracts | 1 | | merged | `ws2-portability` (Sonnet) | [#5](https://github.com/mjtpena/fabricops-digital-agent/pull/5) | Review 1 (13 findings) and verification round (credential guard restored with negative cases) fixed; orchestrator re-ran gates; merged after WS3 with all 209 security tests green |
| WS3 | Identity and API hardening | 1 | | merged | `ws3-identity` (Sonnet) | [#3](https://github.com/mjtpena/fabricops-digital-agent/pull/3) | Review 1 + independent red-team ([comment](https://github.com/mjtpena/fabricops-digital-agent/pull/3#issuecomment-6281103223)) → 19 findings fixed; verification: all fixed, starred fixes proven by revert, 8-process race gives exactly 1 write; 406 tests, 182 security tests |
| WS4 | Delivery pipeline | 1 | A6 (done) for the push dry run | merged | `ws4-delivery` (Sonnet) | [#6](https://github.com/mjtpena/fabricops-digital-agent/pull/6) | f264048: review 1 (15 findings) + verification round (immutable OIDC subjects, environment-first ordering, ruleset integration_id) all fixed; Azure Linux 3 base, Trivy 0 findings; orchestrator re-ran gates green |
| WS5 | Live sandbox estate and live test harness | 1 | D8, A3–A5 | merged | orchestrator's local subagent (Sonnet, shares the `az` login) | [#7](https://github.com/mjtpena/fabricops-digital-agent/pull/7) | d06b957: estate live (4 workspaces, idempotent create/teardown, 6 reversible injectors, 29 live tests); 4/6 injectors open incidents (refresh failure, SLA breach need WS7); 2 adapter mapping bugs + Power BI privacy-marker redaction fixed; review minors fixed; no tenant ids outside the live log; capacity paused. WS7 inputs: generic notebook-exception signature, schema-drift column extraction, signature_match redaction over-match, D9 |
| WS6 | Durable storage and stateless app | 2 | D1, WS1 | in progress | `ws6-storage` (Sonnet) | | |
| WS7 | Signals and detection breadth | 2 | D2, WS1, WS5 | not started | | | |
| WS8 | Notifications and web UI | 2 | D3, WS3 (WS6 preferred) | in progress | `ws8-notify-ui` (Sonnet) | | |
| WS9 | Observability and operations | 2 | WS1 (+WS6) | in progress | `ws9-observability` (Sonnet) | | |
| WS10 | LLM reasoner in production | 3 | D7, WS5, WS7 | not started | | | |
| WS11 | Supervised write path | 3 | WS3, WS6 | not started | | | Carries forward from WS3 verification: reconcile approvals stuck in `EXECUTING` after a crash (visibility + alert); audit the execution claim before the write and record cancellation as `UNKNOWN`; claim before moving the incident to REMEDIATING; log a failed final state/version check; decide whether a policy tightening invalidates earlier lower-role approvals (as risk escalation does) |
| WS12 | Independent security review | 3 | Waves 1–2 | not started | | | |
| W4 | Cut-over: deploy, game day, portability rehearsal, soak, release | 4 | all | not started | orchestrator | | |

Hot files (additive changes only; merged in dependency order): `src/orqentis/bootstrap.py`,
`src/orqentis/config/settings.py`, `.env.example`, `infra/bicep/main.bicep`, `README.md`,
`docs/architecture.md`, `docs/security.md`.

## 3. Decisions

| Id | Question | Recommended default | Decision | Date | ADR |
|---|---|---|---|---|---|
| D0 | Tenant scope; which existing workspaces may be monitored | Treat the tenant as an organisation tenant. Monitor only the sandbox workspaces FabricOps creates | Accepted: organisation tenant; monitor only programme-created sandbox workspaces | 2026-10-20 |  |
| D1 | Production storage (supersedes the open question in ADR-0003) | PostgreSQL Flexible Server (Entra-only, managed identity) for incidents, approvals, dependencies, events, leases and audit chain head; Blob/ADLS Gen2 with version-level immutability for evidence and reports behind one ADLS-API adapter (OneLake-capable); Eventhouse optional sink only | Accepted as recommended | 2026-10-20 | [0008](adr/0008-production-storage.md) |
| D2 | Capacity signal source; schema drift via Delta `_delta_log` | Capacity Metrics semantic model via `executeQueries`, confirmed by a WS7 spike; schema drift from `_delta_log` over OneLake for contract-declared tables with the minimum read grant | Accepted as recommended (capacity source confirmed by a WS7 spike) | 2026-10-20 | [0009](adr/0009-capacity-and-schema-signals.md) |
| D3 | Human interface | Notifier port (Teams Workflows webhook + generic webhook); minimal server-rendered UI behind Easy Auth; Teams Adaptive Card approvals deferred | Accepted as recommended | 2026-10-20 | [0010](adr/0010-human-interface.md) |
| D4 | Deployment model | Single-tenant, one deployment per customer tenant/subscription. Here: `rg-fabricops-{dev,test,prod}` in the *Sandbox Environment 2026* subscription, Australia East | Accepted as recommended | 2026-10-20 | [0011](adr/0011-deployment-model-and-environments.md) |
| D5 | Monthly cost cap; approval for billable resources | Cap proposed in the kickoff message; budget with 50/80/100% alerts | Accepted: USD 750/month. Billing currency is AUD, so the budget is AUD 1,100 (≈ USD 750) with alerts at 50/80/100% actual and 100% forecast to the owner's work email. A1, A2 withdrawn (D8); A3–A6 approved | 2026-10-20 |  |
| D6 | Merge authority | Orchestrator merges workstream PRs after green CI and the review gate; owner approves every `prod` deploy and `v1.0.0`; owner does repo-admin settings | Accepted as recommended | 2026-10-20 |  |
| D7 | LLM resource and model | New dedicated Azure OpenAI account in Australia East; `gpt-5.5` GlobalStandard; WS10 compares newer deployments on the golden set | Accepted as recommended | 2026-10-20 | [0012](adr/0012-llm-hosting.md) |
| D9 | How FabricOps reads semantic-model refresh failures (live finding: refresh history needs dataset **Write**, i.e. workspace Contributor; Read/Build cannot be granted to service principals; Fabric job API does not cover semantic models; Viewer can read a single refresh by request id) | Do **not** give the reader identity write. Primary: Fabric workspace monitoring (semantic-model logs in the monitoring Eventhouse, read with Viewer), validated by a WS7 spike; pipeline-triggered refreshes already surface as pipeline activity failures. Fallback: read-only admin API (refreshables) via a dedicated security group in the *SP read-only admin APIs* tenant setting (tenant change, owner approval) | **Accepted:** the reader identity never gets write. Primary source: Fabric workspace monitoring, validated by a WS7 spike. The read-only admin API is a fallback only with the owner's further approval | 2026-10-20 | |
| D8 | Sandbox Fabric capacity | New F2 `fabricopssandbox` | **Use the existing `fabricdemocap2026` (F4, Australia East, `RG-FABRICDEMO`, *Sandbox Environment 2026*). The orchestrator may resume it for live work and pause it when idle.** Consequences: USD 0.84/h while running; pausing also takes its 4 other workspaces offline; their load is part of the capacity signals; no new capacity is created (A1 and A2 withdrawn) | 2026-10-20 | |

## 4. Blockers and risks

| Id | Item | Owner | Status |
|---|---|---|---|
| B1 | **GitHub Actions jobs do not start:** "recent account payments have failed or your spending limit needs to be increased" (run 39342626445). Every PR's review gate needs green CI | repo owner | resolved 2026-10-20: repository made public on the owner's instruction (secret scan of all branches and history first: no secrets) |
| B2 | Repository default branch was `claude/amazing-feynman-sbw9fs`, not `main` | orchestrator (owner-approved) | resolved 2026-10-20: default branch is `main` |
| R1 | `ubuntu-latest` migrates to Ubuntu 26 from 2026-10-19. WS4 pins runner images | WS4 | open |
| R2 | The tenant hosts other production subscriptions and workspaces. All FabricOps resources stay in prefixed, tagged resource groups and workspaces | orchestrator | open |
| R3 | "Service principals can call Fabric public APIs" is enabled for the whole organisation, not a scoped group. FabricOps docs recommend a scoped group; changing it is the tenant owner's call | owner | accepted for now |
| R4 | The sandbox capacity (D8) is shared with 4 non-programme workspaces. Capacity signals include their load, and reading the Capacity Metrics app's workspace (not created by the programme) needs the owner's approval before the WS7 spike | orchestrator | open |
| R5 | With the external-authenticated ingress default, the app trusts `X-MS-CLIENT-PRINCIPAL`; any Easy Auth gap (first-deploy window, auth disabled by mistake) would let a forged header act as a human admin. Mitigation now: deploy `internal` first, `allowedApplications`. Follow-up: in-app bearer-token validation, raised to WS12 | WS3 / WS12 | open |
| R6 | Cookie (ID-token) sessions in the WS8 UI need CSRF protection: JSON-only state-changing routes and an Origin check | WS8 | open |
| R7 | Approval decisions and single-use execution were not atomic in pre-existing code (5 concurrent executes → 5 writes; approve/reject race lost the rejection), and execute did not re-check the current policy's role. Invariant 7 gap found by the WS3 red-team; being fixed in PR #3 with compare-and-set transitions. No live write was possible (rest mode holds the disabled write client) | WS3 | fixed in PR #3 (verified; merges with it) |
| R8 | Until WS6, the old and new revisions overlap during a deploy and both run schedulers against the same files (audit chain fork risk). Nothing is deployed before WS6; interim mitigations in WS1 | WS1 / WS6 | open |
| R10 | `infra/bicep/README.md` §1: `az ad app update --set api.requestedAccessTokenVersion=2` fails on a brand-new registration ("Couldn't find 'api'"); setting it through Microsoft Graph works. Fix the documented script before the portability rehearsal | orchestrator | open |
| R9 | WS1 (≈4.3k lines) and WS2 (≈5.7k lines) exceed the ≈1.5k-line PR guideline (much of it tests and docs). Accepted as single PRs to avoid re-splitting cost; reviewed in full | orchestrator | accepted |

## 5. Cost

Monthly cap: USD 750 (D5), implemented as Azure budget `fabricops-programme` = AUD 1,100/month (billing
currency AUD) over `rg-fabricops-*` and `RG-FABRICDEMO` (the shared sandbox capacity's group, which also holds
two small non-programme resources, so the budget errs on the early side).

| Resource | Rate | Notes |
|---|---|---|
| `crfabricopsshared` ACR Basic | USD 0.1666/day (≈ USD 5/month) | created 2026-10-20 |
| `fabricdemocap2026` F4 (shared, D8) | USD 0.84/h while running | paused when idle; resume/pause logged in `docs/live-environment.md` |

Spend to date (programme resources): ≈ USD 2: sandbox capacity ≈ 2.0 h at USD 0.84/h (WS5) plus the registry (≈ USD 0.17/day). `RG-FABRICDEMO` month-to-date at kickoff: AUD 9.60 (pre-programme).

## 6. Change history

| Date | Change |
|---|---|
| 2026-10-20 | Kickoff: repo verified, live tenant discovered read-only, WS0 opened, ledger created |
| 2026-10-20 | D8: sandbox uses the existing F4 `fabricdemocap2026`, with resume/pause authorised |
| 2026-10-20 | Wave 1 merged to `main` in dependency order: #1 WS0, #2 ledger, #3 WS3, #5 WS2, #4 WS1, #6 WS4, #7 WS5. Repository made public (B1). Base image moved to MCR Azure Linux 3 after ECR Public also rate-limited runners |
| 2026-10-20 | Wave 2 started: owner approved B1–B9 and decided D9; WS6, WS8, WS9 sessions started; GitHub environments and rulesets, dev API registration, dev deploy and what-if identities and repository variables configured; first CD run to dev |
| 2026-10-20 | WS5 (#7) approved pending CI; D9 raised (refresh history needs dataset Write) |
| 2026-10-20 | All Wave 1 PRs (#3–#6) approved after review, independent red-team (WS3) and verification rounds; merges wait on CI (B1). Merge order: #1, #2, #3, #5, #4, #6 |
| 2026-10-20 | Wave 1 first-round reviews done (4 deep reviews + independent WS3 red-team); fixes sent back to each session |
| 2026-10-20 | Wave 1 started: WS1–WS4 sub-sessions, WS5 live subagent |
| 2026-10-20 | D0–D7 accepted; ADRs 0008–0012; default branch set to `main`; budget, shared ACR, CI/CD OIDC identity and test service principal created (A4–A6) |

## 7. Wave 2 approvals (approved by the owner 2026-10-20)

Prices: Azure retail list prices, Australia East, USD; billing in AUD.

| # | Action | Why | Cost | Undo |
|---|---|---|---|---|
| B1 | `rg-fabricops-dev` via CD (main.bicep + env/dev.bicepparam): Log Analytics + App Insights, reader and actor managed identities, Container Apps env + app (1 replica, 0.5 vCPU/1 GiB), evidence storage (Entra-only, immutability), PostgreSQL Flexible B1ms (Entra-only, PITR 7 d), Key Vault | First deployed environment: WS6 storage, WS8 notifications, WS9 alert test, WS11 later | ~USD 45/month (Container Apps idle ~12, PostgreSQL ~24, logs ~5, storage/KV ~1) | `az group delete -n rg-fabricops-dev` |
| B2 | Entra app registration `FabricOps API (dev)` (Easy Auth audience, app roles Operator/PlatformAdmin/Reader/Automation, idtyp optional claim); assign you PlatformAdmin+Operator; Automation role to the dev deploy identity for smoke tests | Authenticated API and UI | 0 | `az ad app delete --id <appId>` |
| B3 | Entra app `fabricops-deploy-dev` with federated credential `environment:dev` (immutable subject); Contributor on `rg-fabricops-dev`; RBAC Administrator on `rg-fabricops-dev` and `crfabricopsshared` constrained to the template's role ids, no deletes, ServicePrincipal only; custom role "FabricOps Nested Deployment" (`Microsoft.Resources/deployments/*`) on `rg-fabricops-shared`. Entra app `fabricops-whatif` with `pull_request` subject: Reader + what-if on `rg-fabricops-dev` | CD deploys dev; PR what-if | 0 | delete apps, role assignments, custom role |
| B4 | GitHub (repo admin): environments `dev`, `test`, `prod`, `release` (required reviewers on test/prod/release, deployment branch `main` only); rulesets for `main` (PR required, 6 required checks bound to GitHub Actions, no bypass, no force push/delete) and `v*` tags; repository/environment variables (ids only, no secrets) | Protect promotion and releases | 0 | delete rulesets/environments/variables |
| B5 | Fabric: dev reader managed identity gets Viewer on the 4 sandbox workspaces and the OneLake data-access Read role on the sandbox source table | Rest-mode collection in dev | 0 | remove role assignments |
| B6 | Teams: you create a Workflows webhook in a chat or channel and send me the URL; I store it only in the dev Key Vault | WS8 live notification test | 0 | delete the workflow and the secret |
| B7 | Azure Monitor action group (email to your work address) and the WS9 alert rules in dev; deliberate failures in dev to fire each alert | WS9 acceptance | ~USD 1–3/month for scheduled query rules | delete with the RG |
| B8 | (R4) Viewer for the dev reader identity on the existing "Microsoft Fabric Capacity Metrics" workspace (non-programme; capacity-level metadata only) | Validate the WS7 capacity collector live | 0 | remove the role assignment |
| B9 | Resume/pause `fabricdemocap2026` for Wave 2 live tests and the game-day rehearsal (already authorised by D8; listed for cost) | Live validation | USD 0.84/h while running | pause |
