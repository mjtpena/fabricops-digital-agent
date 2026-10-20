# Orqentis FabricOps

**An AI reliability engineer for Microsoft Fabric estates.** MVP v0.1 · Python 3.12+

Orqentis FabricOps is an operational system, not a chatbot. It observes a Fabric estate, detects
incidents with deterministic rules, collects evidence through typed read-only tools, maps
dependencies and blast radius across workspaces and environments, and has an investigator agent
(Microsoft Agent Framework) reason over that evidence. It then retrieves the matching
version-controlled runbook and recommends remediation behind a policy and approval boundary. Every
step is recorded as an auditable incident.

The MVP is **read-only by default**. No Fabric resource can change unless the mutation passes the
policy engine, a human with the required role approves that exact action, and the deployment has
the write capability enabled.

```
make demo
```

This runs the complete synthetic incident from the spec. It needs no Fabric tenant, Azure
subscription or LLM credentials.

---

## What the demo shows

Scenario: at 06:02 the pipeline `ingest_sales_prod` fails in `transform_sales` because
`expected column "customer_id" is missing`. The upstream CRM feed renamed the column, and
`sales_gold` must be available before 06:30 (Australia/Sydney).

```
1.  Simulated telemetry loaded    18 events (1 ACTIVITY_RUN_FAILED, 4 CAPACITY_METRIC, 1 PIPELINE_RUN_FAILED, ...)
2.  Incident detected             INC-2026-0042 · Pipeline ingest_sales_prod failed · HIGH · PROD
3.  Evidence collected            19 evidence items from 17 read-only tool calls (logged, audited, traced)
4.  Investigation executed        FabricOpsInvestigator (rule_based): 7 findings accepted, 0 rejected
                                  Root cause SCHEMA_DRIFT · confidence HIGH (0.95)
                                  Upstream schema change: column 'customer_id' was removed from sales_orders_raw
                                  in SalesBronze at 2026-10-05 06:01 AEDT and 'client_id' was added, consistent
                                  with a rename. transform_sales still expects the original column and failed.
5.  Dependency impact calculated  6 downstream asset(s) across 2 workspace(s): SalesGold, SalesSemanticModel,
                                  sales_gold, ExecutiveSalesReport, FinanceSemanticModel, MonthEndCloseReport
                                  · cross-workspace
                                  SLA: sales_gold AT_RISK
6.  Runbook matched               RB-SCHEMA-DRIFT-001 v1.3.0 'Upstream schema drift breaks a pipeline
                                  transformation' (score 0.93)
7.  Recommendation produced       Risk MEDIUM_RISK · approval required: yes · incident AWAITING_APPROVAL
                                  · Update the notebook schema mapping [transform_sales] -> MEDIUM_RISK, REQUIRE_APPROVAL
                                  · Rerun the pipeline [ingest_sales_prod] -> LOW_RISK, REQUIRE_APPROVAL
8.  Incident report rendered      var/demo/evidence/INC-2026-0042/report.md
9.  Audit trace persisted         var/demo/audit/audit.jsonl · 28 records · hash chain valid

Approval boundary check (the agent attempts mutations directly)
  - Agent calls request_pipeline_rerun -> ACTION_REQUIRES_APPROVAL (approval APR-…; executed=False)
  - Agent tries to execute APR-… -> BLOCKED PRINCIPAL_NOT_HUMAN
  - Agent requests DELETE_ITEM -> ACTION_DENIED (POL-001-deny-delete)
  - Agent calls a non-allowlisted tool approve_action -> NOT_ALLOWED
  - Write client calls: 0
```

The full incident report is printed afterwards. Every statement in it is labelled **FACT**,
**INFERENCE** or **RECOMMENDATION** and cites evidence ids. Three more scenarios run with
`make demo-all` or `make demo SCENARIO=<name>`:

| Scenario | What FabricOps concludes |
|---|---|
| `schema_drift` | Upstream schema change (rename). Code change ruled out from deployment history, capacity ruled out. Cross-workspace blast radius. Similar to INC-2026-0041. |
| `auth_failure` | The service principal client secret expired 14h before the failure. The secret embedded in the Fabric error is redacted everywhere. Recurrence of INC-2026-0012. |
| `capacity_pressure` | PROD F64 saturated (117% CU, background rejection). The contending workload is a DEV notebook on the PROD capacity. Failure on 3 consecutive Mondays between 05:51 and 06:04. |
| `unknown_failure` | Root cause undetermined, LOW confidence. The recent deployment is surfaced as a possible factor without being asserted as the cause. Read-only escalation; no change proposed. |

## Architecture

```mermaid
flowchart TB
  subgraph Fabric["Microsoft Fabric (read-only)"]
    API[Fabric / Power BI REST]
  end
  API --> C[Collectors] --> N[Normalization] --> ES[(Operational Event Store)]
  ES --> D[Detection rules] --> INC[Incident]
  INC --> WF[Pipeline-failure workflow<br/>deterministic, 20 steps]
  WF -->|typed read-only tools| T[Tool registry<br/>allowlist · validation · audit]
  T --> EV[(Evidence store)]
  WF --> AG[FabricOps Investigator<br/>Microsoft Agent Framework]
  AG -->|read-only tools| T
  AG --> G[Grounding validator]
  G --> CF[Deterministic confidence]
  CF --> RB[Runbook retrieval] --> REC[Recommendation]
  REC --> P[Policy engine] --> AP[Approval]
  AP -->|human, role, unexpired, fingerprint match| GW[Action gateway] -->|write capability| W[Fabric write client]
  WF --> R[Incident report] --> EV
  T & P & AP & GW --> AU[(Hash-chained audit trail)]
```

Detection, evidence, reasoning, decision and action are kept separate:

* **The LLM never monitors.** Collectors and detection rules are deterministic code.
* **The agent reasons over evidence and nothing else.** A finding is accepted only if it cites
  evidence ids that exist. Fabricated ids, invented runbooks and uncited root causes are rejected
  and recorded on the agent decision.
* **Confidence is computed, not asked for.** It is a weighted combination of error signature,
  runbook, history, dependency, temporal and corroboration signals. Every factor is stored.
* **Policy and approval live outside the LLM.** Mutation tools return `ACTION_REQUIRES_APPROVAL`
  or `ACTION_DENIED` and never execute. Only a human with the required role can approve. Execution
  re-checks the approval, its expiry, the action fingerprint, the policy and the write capability.
* **Fabric metadata is untrusted input.** It is sanitised, scanned for instruction-like content and
  passed to the model inside an explicit untrusted-data envelope.

See [docs/architecture.md](docs/architecture.md) for the full design and the ADRs in
[docs/adr](docs/adr).

## Quick start

```bash
make demo                         # schema-drift incident end to end (creates .venv on first run)
make demo SCENARIO=capacity_pressure
make test                         # unit, integration, scenario and security tests
make check                        # ruff, ruff format, mypy --strict, tests, runbook validation
make serve                        # REST API on http://127.0.0.1:8000, OpenAPI UI at /docs
```

On a machine where `python3` is older than 3.12, use `make demo PYTHON=python3.12`.

### REST API

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness (anonymous; version and mode) |
| GET | `/health/ready` | Readiness: `503` (fixed reason code and exception type, never a message) until the container is built and the estate catalog is loaded |
| GET | `/system/info` | Like `/health` plus whether the write capability is enabled (needs a FabricOps role) |
| GET | `/incidents` | List incidents (`?status=`) |
| GET | `/incidents/{id}` | Full incident: evidence, findings, root causes, blast radius, timeline |
| POST | `/incidents/{id}/investigate` | Queue the investigation workflow: `202` with a job (idempotent per incident) |
| GET | `/investigations/{job_id}` · `/incidents/{id}/investigation` | Job status (`QUEUED`/`RUNNING`/`SUCCEEDED`/`FAILED`), timestamps and result summary |
| GET | `/incidents/{id}/evidence` | Evidence |
| GET | `/incidents/{id}/recommendations` | Recommendations with policy decisions |
| GET | `/incidents/{id}/report` | Incident report (`?format=markdown`) |
| GET | `/runbooks`, `/runbooks/{id}` | Runbook catalog |
| GET | `/estate/workspaces` | Workspaces |
| GET | `/estate/items/{id}/dependencies` | Upstream, downstream and blast radius |
| GET | `/approvals` | Approval requests (`?incident_id=&status=`) |
| POST | `/approvals/{id}/approve` · `/reject` | Human decision (role-checked, never the requester or an agent) |
| POST | `/approvals/{id}/execute` | Execute an approved action (refused while the write capability is closed) |
| POST | `/telemetry/collect` | Run collectors and detection |
| GET | `/audit`, `/audit/verify` | Audit trail and hash-chain verification |

Locally, identify yourself with headers: `X-Orqentis-User: you@contoso.com` and
`X-Orqentis-Roles: FabricOps.Operator`. In deployed environments the API uses Microsoft Entra ID
through Container Apps built-in authentication (`ORQENTIS_API_AUTH_MODE=easy_auth`).

Callers are classified from their token claims: **app-only tokens (service principals, managed
identities) are services and can never approve, reject or execute**, whatever roles they carry.
Four app roles control access: `FabricOps.Operator` and `FabricOps.PlatformAdmin` (people; approve),
`FabricOps.Automation` (applications only: read endpoints and `POST /telemetry/collect`) and
`FabricOps.Reader` (read-only). Under Easy Auth a caller needs one of them. Request bodies over
`ORQENTIS_API_MAX_BODY_BYTES` get 413, and mutating or expensive calls are rate limited per principal
(429 with `Retry-After`). See [docs/security.md](docs/security.md) and
[ADR-0015](docs/adr/0015-principal-classification-and-api-hardening.md).

### Autonomous operation

In deployed environments (`ORQENTIS_DEPLOYMENT_ENVIRONMENT` `dev`/`test`/`prod`) the app runs a small
in-process scheduler, held by a leader lease, so nobody has to call the API for it to work: it
collects telemetry and runs the detection rules every 5 minutes (incrementally, from per-collector
watermarks), investigates new incidents as background jobs, records SLA state changes as events, and
reloads the estate catalog hourly, applying and auditing what changed. All Fabric calls share one
concurrency and rate budget. Locally it is off unless you set `ORQENTIS_SCHEDULER_ENABLED=true`.
Automation stops at the approval boundary: it can investigate and *request* approval, never approve
or execute. Deployed environments keep their state in the PostgreSQL system of record and Blob storage
(`ORQENTIS_STORAGE_BACKEND=postgres`, [ADR-0017](docs/adr/0017-postgresql-system-of-record.md)), so the app is stateless: it
runs as two or more replicas, revisions may overlap during a deploy, one replica holds the scheduler lease, and there is
one valid audit chain. Locally and in `make demo` the SQLite/filesystem/JSONL adapters are used and nothing needs
Azure. See [ADR-0013](docs/adr/0013-scheduler-lease-and-async-investigation-jobs.md) and
[docs/architecture.md](docs/architecture.md#13-autonomous-runtime).

### Using the LLM investigator

The deterministic reasoner is the default, so the demo and the tests run offline. To reason with
Azure OpenAI through Microsoft Agent Framework, set:

```bash
ORQENTIS_REASONER=agent_framework
ORQENTIS_AZURE_OPENAI_ENDPOINT=https://<resource>.openai.azure.com
ORQENTIS_AZURE_OPENAI_DEPLOYMENT=<deployment>
```

Authentication uses Microsoft Entra ID (`DefaultAzureCredential`); no API keys are involved. The
agent gets the same allowlisted tools, and its output passes the same grounding validation,
confidence model and policy boundary. If the model fails or returns invalid output, the
deterministic reasoner takes over, and the decision records the fallback.

### Connecting to a real Fabric tenant (read-only)

```bash
ORQENTIS_FABRIC_MODE=rest
ORQENTIS_CONTRACTS_PATH=contracts/estate.json   # environments, sources, dependencies, data products, SLAs
ORQENTIS_AUTH_MODE=managed_identity             # or default / workload_identity / service_principal
```

The contracts file is drafted, checked and reviewed with tools, not written from scratch:
`orqentis contracts scaffold` drafts it from a read-only listing (environments inferred from names and marked
for review, `TRIGGERS` edges from observed pipeline runs, placeholders for owners and SLAs),
`orqentis contracts validate` checks it (schema, references, time zones, with line numbers), and
`schemas/contracts.schema.json` gives editors completion. A workspace's **environment** can be declared by id
or name pattern and then overrides the name-based guess, which drives incident severity and policy. A Fabric
tenant in another Entra tenant is reached with a service principal whose secret is a Key Vault reference,
and sovereign clouds are configuration (`ORQENTIS_AUTHORITY_HOST` and the token scopes). In a deployed environment the
file is a blob that a person uploads and the app only reads, and a running app reloads it without a restart (Bicep
guide, step 4). See [Development](docs/development.md#contracts-workflow) and the [Bicep guide](infra/bicep/README.md).

The operating identity needs read permissions only (see [docs/security.md](docs/security.md)).
Fabric has no public API for capacity utilisation or Delta schema history, so those signals arrive
through the event store; in mock mode they are replayed from fixtures.

## Status

| Milestone | Status |
|---|---|
| 1. Domain model + synthetic Fabric estate | Done |
| 2. Incident + evidence model | Done |
| 3. Dependency graph + blast radius | Done |
| 4. Runbook retrieval | Done |
| 5. FabricOps Investigator agent (Agent Framework + deterministic reasoner) | Done |
| 6. Policy and approval boundary | Done |
| 7. REST API | Done |
| 8. Real Fabric read-only adapter | Done. Mocked-transport tests, tests replaying recorded live responses, and a live suite against the sandbox estate (`docs/live-environment.md`, `tests/live`); Viewer cannot read semantic model refresh history (`docs/security.md` section 11) |
| 9. Durable storage | Done (ADR-0008, ADR-0017): PostgreSQL system of record + immutable Blob evidence behind the storage ports, safe at two or more replicas; Eventhouse stays an optional analytics sink |
| 10. Teams integration | Planned. `ApprovalProvider` port is defined; `LocalApprovalProvider` implemented |

Definition of done (spec §32):

- [x] repository installs locally · [x] tests pass · [x] synthetic Fabric estate loads
- [x] synthetic failed pipeline creates incident · [x] evidence collector retrieves failure evidence
- [x] dependency graph calculates blast radius · [x] previous incidents are searched
- [x] applicable runbook is retrieved · [x] agent produces evidence-grounded diagnosis
- [x] remediation recommendation is generated · [x] policy engine blocks unapproved mutations
- [x] incident report is persisted · [x] audit trail exists · [x] REST API exposes incident
- [x] local demo runs with one command · [x] Azure/Fabric adapters have clearly defined interfaces
- [x] README explains architecture and demo

## Repository layout

```
src/orqentis/
  domain/         typed models (incident, evidence, findings, actions, approvals, SLA, ...)
  fabric/         FabricClient ports, MockFabricClient, read-only REST adapter, write-capability clients
  telemetry/      collectors and normalisation into operational events
  runtime/        leader lease, scheduler, background investigation jobs, readiness supervision
  incidents/      detection, lifecycle, classification, history, confidence, SLA, workflows
  evidence/       evidence recorder
  topology/       estate catalog and storage-agnostic dependency graph
  agents/         FabricOps Investigator, reasoners, prompts, grounding validation
  tools/          tool registry, read-only tools, approval-only mutation tools, untrusted-content handling
  runbooks/       runbook loader and catalog
  policies/       policy engine and default policy (YAML)
  approvals/      approval provider port, local provider, approval rules
  actions/        action gateway (the only path to a mutation)
  storage/        storage ports with local adapters (SQLite, files); postgres/ and blob/ hold the PostgreSQL and Blob adapters
  reports/        incident report model, builder and Markdown renderer
  api/            FastAPI application
  config/         settings and clock
  observability/  structured logging, OpenTelemetry, redaction, audit trail
runbooks/         version-controlled Markdown/YAML runbooks
fixtures/         synthetic estate, scenario telemetry, incident history
tests/            unit, integration, scenarios (incl. critical security tests)
docs/             architecture, security, product, threat model, development, ADRs
infra/bicep/      Azure deployment (Container Apps, managed identities, App Insights, Key Vault references)
contracts/        example contracts file and catalog (orqentis contracts validate)
schemas/          JSON Schema of the contracts file (generated from the model)
```

## Documentation

* [Product](docs/product.md): positioning, persona, scenarios, roadmap
* [Architecture](docs/architecture.md): flow, ports and adapters, workflow, agent, data model
* [Security](docs/security.md): identities, capability boundaries, approval, audit, prompt injection
* [Threat model](docs/threat-model.md): STRIDE analysis and residual risks
* [Development](docs/development.md): setup, configuration, extending runbooks, signatures, scenarios
* [Delivery](docs/delivery.md): CI/CD pipeline, OIDC setup, environments, releases, rollback
* [ADRs](docs/adr): recorded architecture decisions

## Licence

Proprietary. See [LICENSE](LICENSE).
