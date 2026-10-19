# Production readiness ledger

The single record of the programme that takes Orqentis FabricOps from a verified PoC to
production (`v1.0.0`). It is updated after every merge, live action and decision. Evidence is a
link to a PR, a CI run, a log or a document, never a claim.

Status values: `not started`, `in progress`, `blocked`, `done`.

Last updated: 2026-10-20 (kickoff).

## 1. Definition of done

| # | Criterion | Status | Evidence |
|---|---|---|---|
| 1 | **Continuous delivery.** `dev`, `test`, `prod` deployed by GitHub Actions CD over OIDC; no secrets in repo or pipeline; manual approval for `prod`; `main` CI green | not started | |
| 2 | **Autonomous operation.** `rest` mode against the live tenant; scheduled collect, detect, investigate, notify without human prompting | not started | |
| 3 | **Detection on real failures.** Schema drift, notebook failure, pipeline timeout, credential/auth failure, semantic-model refresh failure, SLA breach: each detected within 10 min, investigated automatically, correct category (or honest LOW-confidence "undetermined"), human notified with a report link | not started | |
| 4 | **Stateless and resilient.** 2+ replicas, leader election, zero-downtime deploys, audit chain verifies across replicas and deploys; no Azure Files or SQLite in deployed environments | not started | |
| 5 | **Durable data.** Backups or PITR; one restore rehearsed and documented | not started | |
| 6 | **Operable.** Alerts for app down, restart loop, 5xx rate, collector errors, collection heartbeat (15 min), audit verification failure; test fire done; `docs/operations.md` | not started | |
| 7 | **Secure.** Threat model updated; independent review with every finding fixed or accepted in writing; no unaccepted high/critical in dependency and image scans; SBOM published; app-only tokens can never approve or execute | not started | |
| 8 | **Portable.** Fresh RG and different tenant from docs + parameter file + contracts scaffold; declared environment classification; configurable time zone; sovereign-cloud scopes/authority (unit-tested) | not started | |
| 9 | **Supervised remediation.** `RERUN_PIPELINE` and `REFRESH_SEMANTIC_MODEL` end to end in sandbox DEV via approval flow and a separate executor holding only the actor identity; writes off by default and off in `prod`; two approvers for HIGH_RISK; "no mutation is ever auto-allowed" unchanged | not started | |
| 10 | **LLM reasoner validated.** Golden-set eval against real Azure OpenAI; accuracy ≥ rule-based; 0 ungrounded findings accepted; cost and latency recorded; fallback proven | not started | |
| 11 | **Soak.** 72 h continuous in `test` (or read-only `prod`), no unhandled errors, no unexplained restarts | not started | |
| 12 | **Quality gates.** `make check` passes; `make demo` offline; ADR per architecture change; `v1.0.0` tagged with CHANGELOG | in progress | Local `make check` green on the WS0 branch (235 passed). CI blocked, see B1 |

## 2. Workstreams

At most four sub-sessions run at once. Each workstream is one branch (`prod/<ws-id>-<slug>`) and
one PR to `main`. Briefs live in [`docs/programme/briefs/`](programme/briefs/).

| WS | Title | Wave | Depends on | Status | Owner session | PR | Evidence |
|---|---|---|---|---|---|---|---|
| WS0 | CI fix (Docker Hub 429; Windows local gates) | 0 | | in progress (blocked on B1) | orchestrator | [#1](https://github.com/mjtpena/fabricops-digital-agent/pull/1) | Local gates green; CI not started (B1) |
| WS1 | Autonomous runtime | 1 | | not started | | | |
| WS2 | Portability and contracts | 1 | | not started | | | |
| WS3 | Identity and API hardening | 1 | | not started | | | |
| WS4 | Delivery pipeline | 1 | A6 (OIDC) for the push dry run | not started | | | |
| WS5 | Live sandbox estate and live test harness | 1 | A1–A5 | not started | orchestrator | | |
| WS6 | Durable storage and stateless app | 2 | D1, WS1 | not started | | | |
| WS7 | Signals and detection breadth | 2 | D2, WS1, WS5 | not started | | | |
| WS8 | Notifications and web UI | 2 | D3, WS3 (WS6 preferred) | not started | | | |
| WS9 | Observability and operations | 2 | WS1 (+WS6) | not started | | | |
| WS10 | LLM reasoner in production | 3 | D7, WS5, WS7 | not started | | | |
| WS11 | Supervised write path | 3 | WS3, WS6 | not started | | | |
| WS12 | Independent security review | 3 | Waves 1–2 | not started | | | |
| W4 | Cut-over: deploy, game day, portability rehearsal, soak, release | 4 | all | not started | orchestrator | | |

Hot files (additive changes only; merged in dependency order): `src/orqentis/bootstrap.py`,
`src/orqentis/config/settings.py`, `.env.example`, `infra/bicep/main.bicep`, `README.md`,
`docs/architecture.md`, `docs/security.md`.

## 3. Decisions

| Id | Question | Recommended default | Decision | Date | ADR |
|---|---|---|---|---|---|
| D0 | Tenant scope; which existing workspaces may be monitored | Treat the tenant as an organisation tenant. Monitor only the sandbox workspaces FabricOps creates | pending | | |
| D1 | Production storage (supersedes the open question in ADR-0003) | PostgreSQL Flexible Server (Entra-only, managed identity) for incidents, approvals, dependencies, events, leases and audit chain head; Blob/ADLS Gen2 with version-level immutability for evidence and reports behind one ADLS-API adapter (OneLake-capable); Eventhouse optional sink only | pending | | |
| D2 | Capacity signal source; schema drift via Delta `_delta_log` | Capacity Metrics semantic model via `executeQueries`, confirmed by a WS7 spike; schema drift from `_delta_log` over OneLake for contract-declared tables with the minimum read grant | pending | | |
| D3 | Human interface | Notifier port (Teams Workflows webhook + generic webhook); minimal server-rendered UI behind Easy Auth; Teams Adaptive Card approvals deferred | pending | | |
| D4 | Deployment model | Single-tenant, one deployment per customer tenant/subscription. Here: `rg-fabricops-{dev,test,prod}` in the *Sandbox Environment 2026* subscription, Australia East | pending | | |
| D5 | Monthly cost cap; approval for billable resources | Cap proposed in the kickoff message; budget with 50/80/100% alerts | pending | | |
| D6 | Merge authority | Orchestrator merges workstream PRs after green CI and the review gate; owner approves every `prod` deploy and `v1.0.0`; owner does repo-admin settings | pending | | |
| D7 | LLM resource and model | New dedicated Azure OpenAI account in Australia East; `gpt-5.5` GlobalStandard; WS10 compares newer deployments on the golden set | pending | | |

## 4. Blockers and risks

| Id | Item | Owner | Status |
|---|---|---|---|
| B1 | **GitHub Actions jobs do not start:** "recent account payments have failed or your spending limit needs to be increased" (run 39342626445). Every PR's review gate needs green CI | repo owner | open |
| B2 | Repository default branch is `claude/amazing-feynman-sbw9fs`, not `main` | repo owner | open |
| R1 | `ubuntu-latest` migrates to Ubuntu 26 from 2026-10-19. WS4 pins runner images | WS4 | open |
| R2 | The tenant hosts other production subscriptions and workspaces. All FabricOps resources stay in prefixed, tagged resource groups and workspaces | orchestrator | open |
| R3 | "Service principals can call Fabric public APIs" is enabled for the whole organisation, not a scoped group. FabricOps docs recommend a scoped group; changing it is the tenant owner's call | owner | accepted for now |

## 5. Cost

Monthly cap: pending (D5). Spend to date: USD 0 (no billable resources created).

## 6. Change history

| Date | Change |
|---|---|
| 2026-10-20 | Kickoff: repo verified, live tenant discovered read-only, WS0 opened, ledger created |
