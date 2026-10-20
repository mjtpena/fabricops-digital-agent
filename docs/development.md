# Development

## Setup

Requirements: Python 3.12 or newer and `make`. Nothing else: the demo and the tests need no Azure,
Fabric or LLM access. On Windows, use GNU make from Git Bash; the Makefile detects the `Scripts/`
venv layout and defaults to `python` (override with `PYTHON=...`).

```bash
make install            # .venv + pip install -e ".[dev,llm]"
make demo               # schema_drift scenario
make demo-all           # all scenarios, summary only
make check              # ruff check, ruff format --check, mypy --strict, pytest, runbook validation
```

Without `make`:

```bash
python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev,llm]"
.venv/bin/orqentis demo --scenario schema_drift      # or: python -m orqentis demo
.venv/bin/pytest -q
```

## CLI

| Command | Purpose |
|---|---|
| `orqentis demo [--scenario NAME] [--data-dir var/demo] [--no-report]` | Reset local state, load the synthetic estate and telemetry, detect, investigate, show the approval boundary and print the report |
| `orqentis serve [--host 127.0.0.1] [--port 8000]` | Run the API. In mock mode it loads the scenario and runs detection at start-up |
| `orqentis scenarios` | List synthetic scenarios |
| `orqentis runbooks` | Validate and list runbooks (non-zero exit on invalid runbooks) |
| `orqentis audit [--data-dir DIR]` | Verify the audit hash chain |

Demo artefacts are written to `var/demo/`:

* `evidence/INC-*/`: raw evidence (`*-EVnnn.json`, immutable), `report.md`, `report.json` and
  `trace-<id>.json`.
* `audit/audit.jsonl`: the hash-chained audit trail.
* `logs/orqentis.jsonl`: structured logs.
* `events.sqlite3` and `state.sqlite3`.

## Using the API locally

```bash
make serve
curl -s localhost:8000/incidents -H 'X-Orqentis-User: me@contoso.com' | jq '.[0]'
curl -s -X POST localhost:8000/incidents/INC-2026-0042/investigate -H 'X-Orqentis-User: me@contoso.com' | jq
curl -s localhost:8000/incidents/INC-2026-0042/report?format=markdown -H 'X-Orqentis-User: me@contoso.com'
curl -s -X POST localhost:8000/approvals/<APR-id>/approve \
     -H 'X-Orqentis-User: olivia@contoso.com' -H 'X-Orqentis-Roles: FabricOps.Operator' \
     -H 'content-type: application/json' -d '{"comment":"mapping PR #512 merged"}'
```

Interactive OpenAPI documentation is served at `http://127.0.0.1:8000/docs`.

## Configuration

All settings are environment variables with the prefix `ORQENTIS_`. They are listed with
explanations in [`.env.example`](../.env.example) and defined in `src/orqentis/config/settings.py`.
Settings are validated at start-up. For example, `dev_headers` authentication is refused outside
`local`, `agent_framework` needs a model endpoint, and `rest` mode needs `ORQENTIS_TENANT_ID` and
a contracts file.

### Running the investigator on an LLM

```bash
export ORQENTIS_REASONER=agent_framework
export ORQENTIS_AZURE_OPENAI_ENDPOINT=https://<resource>.openai.azure.com
export ORQENTIS_AZURE_OPENAI_DEPLOYMENT=<deployment>
az login            # DefaultAzureCredential picks up your developer identity
make demo
```

Your identity needs the *Cognitive Services OpenAI User* role on the Azure OpenAI resource. To use
an Azure AI Foundry project instead, set `ORQENTIS_LLM_PROVIDER=foundry`,
`ORQENTIS_FOUNDRY_PROJECT_ENDPOINT` and `ORQENTIS_FOUNDRY_MODEL` (this needs the
`agent-framework-foundry` package).

### Running against a real Fabric tenant (read-only)

```bash
export ORQENTIS_FABRIC_MODE=rest
export ORQENTIS_TENANT_ID=<entra-tenant-id>
export ORQENTIS_CONTRACTS_PATH=contracts/estate.json
make serve
curl -X POST localhost:8000/telemetry/collect -H 'X-Orqentis-User: me@contoso.com'
```

The contracts file declares what Fabric cannot tell you: external sources, dependencies, data
products and SLAs. Its shape matches the corresponding sections of
`fixtures/fabric/estate.json` (`external_sources`, `data_products`, `dependencies`, `slas`).
Capacity utilisation and Delta schema changes have no public REST API. Until the Eventhouse
collectors arrive (milestone 9), the capacity tool reports `CAPABILITY_UNAVAILABLE`, the
investigation continues, and the missing evidence lowers confidence.

Tool calls have a 30s timeout and the HTTP client retries up to `ORQENTIS_HTTP_MAX_RETRIES` times,
honouring `Retry-After` (capped at 60s). When the API is heavily throttled, lower the retries or
raise the timeout so that retries are not cut off by the tool timeout.

## Testing

| Suite | Contents |
|---|---|
| `tests/unit` | Dependency traversal and blast radius, classification and taxonomy extension, runbook loading and matching, confidence, policy decisions, approval enforcement, SLA, deployment comparison, audit chain, redaction and untrusted content, tool registry, detection, incident lifecycle |
| `tests/integration` | Storage adapters; REST API; Microsoft Agent Framework investigator with a scripted model (grounding, injection, fallback, budget); Fabric REST adapter against mocked HTTP (auth scopes, retries, pagination, error mapping, status mapping); REST-mode composition end to end |
| `tests/scenarios` | Schema drift, authentication failure, capacity pressure, unknown failure; critical security boundary (`-m security`); CLI demo |

`tests/support/scripted_chat_client.py` plays the model's side of an Agent Framework
conversation, so the real `Agent`, function invocation and telemetry layers run without an
endpoint. Tests clear `ORQENTIS_*` environment variables and ignore `.env`.

`tests/fixtures/fabric_rest/` holds real Fabric and Power BI responses recorded from the live sandbox
(every identifier remapped to a synthetic GUID, nothing else changed) and
`tests/integration/test_fabric_rest_recorded.py` replays them through the adapter, so the real payload
shapes are covered offline. `tests/unit/test_fixture_sanitiser.py` guards the sanitiser and scans the
committed fixtures for identities, tokens and e-mail addresses.

## Live sandbox (tests against a real Fabric tenant)

`scripts/live-estate/` builds, faults and removes a disposable estate in the FabricOps sandbox tenant, and
`tests/live` runs the read-only REST adapter and the whole application against it as the least-privilege
test service principal. Nothing here runs in CI or in `make check`: the live tests are skipped unless
`ORQENTIS_LIVE_TESTS=1`. The register of what exists, what was created when and how to undo it is
[live-environment.md](live-environment.md); what the read-only identity can and cannot see is
[security.md](security.md) section 10.

You need an `az login` that may create workspaces on the sandbox capacity, plus the test principal's
credentials in `~/.fabricops/live-reader.env` (`AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`,
never in the repository; override the path with `FABRICOPS_LIVE_ENV_FILE`). The scripts read their names
from `scripts/live-estate/config.env.example`, which holds placeholders for the tenant, subscription,
capacity and test-principal ids: copy it to `config.env` (git-ignored) and fill them in; the scripts stop
with a message while one is still a placeholder, and every entry point checks the `az` sign-in is in that
tenant before it reads or changes anything. `python -m tests.live.record_fixtures` rejects fixtures that
contain any name listed in `FABRICOPS_FIXTURE_DENY` (organisation, owner, domain).

```bash
scripts/live-estate/capacity.sh status|resume|suspend   # F4 sandbox capacity, USD 0.84/h while running
scripts/live-estate/create.sh [--env DEV|TEST|PROD|CONSUMER] [--no-run]   # idempotent; also writes contracts.sandbox.json
scripts/live-estate/status.sh                           # read-only inventory
scripts/live-estate/seed-history.sh                     # leave one failed run of each kind in the TEST pipeline
scripts/live-estate/inject/<fault>.sh [--env E] [--revert] [--run|--no-run]
scripts/live-estate/serve-live.sh                       # the app in rest mode as the test principal
ORQENTIS_LIVE_TESTS=1 pytest tests/live                 # the live suite
python -m tests.live.record_fixtures                    # re-record the sanitised fixtures
scripts/live-estate/teardown.sh [--dry-run]             # removes only the four sandbox workspaces and the fake connection
```

The estate: `FabricOps Sandbox [DEV|TEST|PROD]` each hold a lakehouse `crm_source` with a synthetic
`customers` Delta table, the notebooks `seed_customers` and `transform_customers` and the pipeline
`ingest_customers` (which runs `transform_customers` into `customers_clean`); `FabricOps Sandbox Consumer`
holds the Direct Lake semantic model `customers_model` over PROD's `customers_clean` and the report
`customers_report`. `contracts.sandbox.template.json` declares the sources, data products, dependencies and
SLAs for it; `create.sh` fills in the live item ids (`contracts.sandbox.json`, git-ignored because it holds
tenant ids; regenerate it with `create.sh --contracts-only`).

Fault injectors, each reversible with `--revert` (see the header of each script):

| Injector | What it does | What FabricOps sees |
|---|---|---|
| `schema-drift.sh` | renames `customers.customer_id` to `client_id` in the source table | the notebook activity and the pipeline fail with `UNRESOLVED_COLUMN` |
| `notebook-failure.sh` | the transform notebook raises | the activity fails with the exception text nested in the message |
| `pipeline-timeout.sh` | 10 second activity timeout | activity `TimedOut` with an empty error object |
| `auth-failure.sh` | a first activity calls the Microsoft Entra token endpoint through a connection holding a fake credential | an `AADSTS700016` error in the activity |
| `refresh-failure.sh` | the semantic model is repointed at a missing Delta table | a failed refresh (readable only with write permission) |
| `sla-breach.sh` | contracts only: every declared deadline becomes `00:00:01` | `BREACHED` SLA evidence at investigation time |

## Extending

### Add a runbook

Create `runbooks/<area>/<name>.md`:

```markdown
---
id: RB-EXAMPLE-001
version: "1.0.0"
title: ...
category: PIPELINE_TIMEOUT          # a taxonomy category
severity: MEDIUM
signals: ["timed out", "timeout"]   # case-insensitive regexes matched against error text and evidence
applies_to: [DataPipeline, Notebook]
risk: MEDIUM_RISK
approval_required: true
owner: team@contoso.com
last_reviewed: 2026-10-01
remediation_actions:                # optional: structured actions the recommendation proposes
  - action_type: RERUN_PIPELINE
    title: Rerun the pipeline
    target: primary_item            # primary_item | failure_point | affected_semantic_models |
    idempotent: true                # upstream_source | contending_workload | capacity
---
## Symptoms
## Diagnostic Steps
## Possible Causes
## Recommended Remediation
## Validation
## Rollback
## Escalation
```

Run `orqentis runbooks`. Missing sections, invalid regexes, unknown categories and duplicate ids
fail validation.

### Add an error signature or incident category

```python
from orqentis.domain.taxonomy import TAXONOMY
from orqentis.incidents.classification import ErrorSignature

TAXONOMY.register("DBT_TEST_FAILURE", "dbt data test failed")
classifier.register(ErrorSignature(id="SIG-DBT", category="DBT_TEST_FAILURE", description="dbt test failed",
                                   patterns=[r"dbt test .+ failed"], priority=80))
```

### Add a synthetic scenario

Add `fixtures/telemetry/<name>.json` (`scenario`, `pipeline_runs`, `activity_runs`, `refreshes`,
`capacity_snapshots`, `events`), referencing items in `fixtures/fabric/estate.json`. Fixtures are
validated against the domain models, so typos fail fast. Run it with
`orqentis demo --scenario <name>` and add assertions to `tests/scenarios/test_scenarios.py`.

### Add a detection rule or workflow

Implement `DetectionRule` (`incidents/detection.py`) or `InvestigationWorkflow`
(`incidents/workflows/base.py`) and register it in `bootstrap.py`. New workflows reuse the tool
registry, the evidence recorder, the investigator, grounding, confidence, policy and reports.

## Conventions

* `from __future__ import annotations`, strict typing (`mypy --strict`), Pydantic v2 models with
  `extra="forbid"`.
* No Fabric, Azure or LLM SDK imports outside adapters and `bootstrap.py`.
* Every change to architecture gets an ADR in `docs/adr/`.
