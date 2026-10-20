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
| `orqentis contracts validate PATH [--catalog FILE] [--tenant-id ID] [--strict]` | Check a contracts file: JSON syntax, schema, duplicate ids and edges, references, time zones; with a catalog also that every referenced workspace and item exists. One `file:line:column: error: /pointer: message` per problem; non-zero exit on errors (and on warnings with `--strict`) |
| `orqentis contracts scaffold --output FILE [--catalog-output FILE] [--workspace ID_OR_NAME]... [--scanner]` | Draft a contracts file from a live **read-only** listing of the Fabric tenant (see below). A draft for human review, never applied |
| `orqentis contracts schema [--output FILE \| --check FILE]` | Print, write or check the JSON Schema of the contracts file |

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
curl -s -X POST localhost:8000/incidents/INC-2026-0042/investigate -H 'X-Orqentis-User: me@contoso.com' | jq   # 202 + job
curl -s localhost:8000/incidents/INC-2026-0042/investigation -H 'X-Orqentis-User: me@contoso.com' | jq          # poll the job
curl -s localhost:8000/incidents/INC-2026-0042/report?format=markdown -H 'X-Orqentis-User: me@contoso.com'
curl -s -X POST localhost:8000/approvals/<APR-id>/approve \
     -H 'X-Orqentis-User: olivia@contoso.com' -H 'X-Orqentis-Roles: FabricOps.Operator' \
     -H 'content-type: application/json' -d '{"comment":"mapping PR #512 merged"}'
```

Interactive OpenAPI documentation is served at `http://127.0.0.1:8000/docs`.

`orqentis serve` binds to `127.0.0.1` by default and refuses the self-asserted dev headers on any other address.
To test the container image locally, opt in explicitly and publish the port on loopback only:

```bash
docker run --rm -p 127.0.0.1:8000:8000 -e ORQENTIS_API_AUTH_MODE=dev_headers \
  -e ORQENTIS_DEV_HEADERS_ALLOW_REMOTE=true orqentis-fabricops:local
```

Without those two variables the image uses `easy_auth`: `GET /health` answers and every other route returns 401.

Locally the API only checks the roles that approvals need. To try the production role model (every call
needs `FabricOps.Reader`, `FabricOps.Automation`, `FabricOps.Operator` or `FabricOps.PlatformAdmin`), start the
server with `ORQENTIS_API_REQUIRE_READ_ROLE=true` and add `-H 'X-Orqentis-Roles: FabricOps.Reader'`. The
`dev_headers` mode always produces a person; service principals only exist behind Easy Auth (see
`tests/support/easy_auth.py` for principals shaped like the ones Container Apps forwards). Request bodies over
`ORQENTIS_API_MAX_BODY_BYTES` get 413, and mutating or expensive calls are rate limited (429 + `Retry-After`).

## Configuration

All settings are environment variables with the prefix `ORQENTIS_`. They are listed with
explanations in [`.env.example`](../.env.example) and defined in `src/orqentis/config/settings.py`.
Settings are validated at start-up. For example, `dev_headers` authentication is refused outside
`local`, `agent_framework` needs a model endpoint, and `rest` mode needs `ORQENTIS_TENANT_ID` and
a contracts file.

### Running the scheduler locally

The scheduler (collect + detect, auto-investigation, SLA sweep, catalog refresh) is off in `local`.
Turn it on to watch the app work on its own:

```bash
ORQENTIS_SCHEDULER_ENABLED=true ORQENTIS_SCHEDULER_TICK_SECONDS=2 make serve
# the seeded incident is investigated without any API call:
curl -s localhost:8000/incidents/INC-2026-0042/investigation -H 'X-Orqentis-User: me@contoso.com' | jq .trigger   # "SCHEDULER"
curl -s localhost:8000/health/ready                                                                              # {"status":"ready","scheduler":"leader"}
```

In mock mode the clock is frozen at the scenario's "now" (`ORQENTIS_CLOCK=scenario`), so each job runs
once; use `ORQENTIS_CLOCK=system` to see jobs recur. Tests never wait for real time: they advance a
`FixedClock` and call `Scheduler.tick()` (see `tests/unit/test_scheduler.py`).

### Looking at the metrics locally

Metrics are recorded through OpenTelemetry and exported nowhere by default. To see them, print them:

```bash
ORQENTIS_OTEL_CONSOLE_EXPORTER=true orqentis demo --no-report   # spans, then the metrics on exit
OTEL_METRIC_EXPORT_INTERVAL=5000 ORQENTIS_OTEL_CONSOLE_EXPORTER=true make serve   # every 5 seconds
```

The instruments and their attributes are listed in `docs/operations.md` and in `src/orqentis/observability/metrics.py`;
tests read them back from an in-memory reader (`tests/support/metrics.py`). `ORQENTIS_LLM_INPUT_COST_PER_MILLION_TOKENS` /
`ORQENTIS_LLM_OUTPUT_COST_PER_MILLION_TOKENS` turn the token counts into the estimated-cost metric.

### Running the web UI and notifications locally

`make serve` serves the UI at `http://127.0.0.1:8000/ui`. A browser cannot send the `X-Orqentis-User` / `X-Orqentis-Roles` development
headers, so use a header-injecting browser extension, or `curl -H 'X-Orqentis-User: you@contoso.com' -H 'X-Orqentis-Roles: FabricOps.Operator'
http://127.0.0.1:8000/ui/incidents`. A decision form posts with an `Origin` header, which browsers add; the CSRF token comes from the page.

Notifications are off (`ORQENTIS_NOTIFIER=null`). The SSRF guard refuses loopback and private addresses on purpose, so a local receiver cannot be the
target; use a public request-inspection endpoint for the generic webhook (`ORQENTIS_NOTIFIER=webhook`, `ORQENTIS_NOTIFY_WEBHOOK_URL`,
`ORQENTIS_PUBLIC_BASE_URL=http://localhost:8000`), or a Teams Workflows webhook ([docs/teams-notifications.md](teams-notifications.md)), and check the setup
with `orqentis notify-test`. The tests use fakes and `httpx.MockTransport`; nothing in them touches the network.

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

The contracts file declares what Fabric cannot tell you: which environment a workspace is, external
sources, dependencies, data products and SLAs. Its shape is described by
[`schemas/contracts.schema.json`](../schemas/contracts.schema.json) and matches the corresponding sections
of `fixtures/fabric/estate.json` (`external_sources`, `data_products`, `dependencies`, `slas`).
[`contracts/estate.example.json`](../contracts/estate.example.json) is a small worked example. If the file
is missing or invalid, startup fails with one error that names the path and every violation, so run
`orqentis contracts validate` before you deploy it (see *Contracts workflow*).
Capacity utilisation and Delta schema changes have no public REST API. Until the Eventhouse
collectors arrive (milestone 9), the capacity tool reports `CAPABILITY_UNAVAILABLE`, the
investigation continues, and the missing evidence lowers confidence.

Tool calls have a 30s timeout and the HTTP client retries up to `ORQENTIS_HTTP_MAX_RETRIES` times,
honouring `Retry-After` (capped at 60s). When the API is heavily throttled, lower the retries or
raise the timeout so that retries are not cut off by the tool timeout.

### Other clouds and tenants

* **Sovereign clouds.** `ORQENTIS_AUTHORITY_HOST` (an https host, for example
  `https://login.microsoftonline.us`) is passed to every azure-identity credential that talks to Entra ID,
  and `ORQENTIS_FABRIC_SCOPE` / `ORQENTIS_POWERBI_SCOPE` replace the public-cloud token scopes.
  `ORQENTIS_FABRIC_API_BASE_URL` / `ORQENTIS_POWERBI_API_BASE_URL` point at the cloud's APIs. Unset means the
  public cloud. The endpoint sets Microsoft documents for Power BI (public, US Government GCC / GCC High / DoD,
  China) are in [`infra/bicep/README.md`](../infra/bicep/README.md#sovereign-clouds); Fabric availability and
  endpoints differ by cloud, so check them for yours. Nothing was exercised against a live sovereign cloud.
* **A Fabric tenant in another Entra tenant.** Use `ORQENTIS_AUTH_MODE=service_principal` with
  `AZURE_TENANT_ID`, `AZURE_CLIENT_ID` and one of `AZURE_CLIENT_SECRET` / `AZURE_CLIENT_CERTIFICATE_PATH` (a PEM
  file) taken from a secret store, and `ORQENTIS_LLM_AUTH_MODE=managed_identity` so Azure OpenAI keeps using
  the managed identity. The app refuses to start, naming the missing variables, if the inputs are incomplete.
  The Bicep side (Key Vault references) is in the *Cross-tenant Fabric access* section of the Bicep README.
* **Time zone.** `ORQENTIS_DEFAULT_TIMEZONE` must be an IANA zone; an unknown zone fails at startup.

## Contracts workflow

The contracts file is reviewed like code. Tools draft and check it; a person decides what is true.

1. **Draft** from a read-only listing. This reads workspaces, items, recent pipeline runs and their
   activity runs through the same read-only Fabric client the app uses, and writes two files:

   ```bash
   export ORQENTIS_TENANT_ID=<fabric-tenant-id>      # and sign in as a reader, e.g. `az login`
   orqentis contracts scaffold --output contracts/estate.json --catalog-output contracts/catalog.json
   ```

   The draft contains:

   | Section | What is drafted | How to treat it |
   |---|---|---|
   | `environments` | One entry per workspace, from its name (`Sales [PROD]` is PROD), each marked `"inferred": true`. A name with no environment becomes `UNKNOWN` | **Not enforced** while `inferred` or `UNKNOWN` (the name still decides). Confirm each one, fix wrong ones, then delete `"inferred"` |
   | `dependencies` | `TRIGGERS` edges (notebook or pipeline) and `REFRESHES` edges (semantic model) observed in the `input` of recent pipeline activity runs (`--max-runs`, default 5 per pipeline) | Real observations, but only of what ran recently; add the rest |
   | `external_sources`, `data_products`, `slas` | **Placeholders** marked `"metadata": {"draft": true}`: `TODO` owners, one data product per workspace holding a lakehouse or warehouse, a 06:00 deadline | Fill in or delete, then remove `draft` |

   Lakehouse and warehouse targets of activities (a copy activity may read or write them) are *not*
   turned into edges; the command reports how many, so add `READS_FROM` / `WRITES_TO` by hand. The command
   refuses to overwrite existing files without `--force`, and `--workspace` limits the listing.

   **Lineage from the admin Scanner API (optional, interactive, administrators only).** `--scanner` adds
   report to semantic-model, semantic-model to upstream-model and semantic-model to data-source edges from the
   Power BI admin Scanner API (`/admin/workspaces/getInfo`, `scanStatus`, `scanResult`). It needs a signed-in
   Fabric administrator: it opens a browser sign-in (`--scanner-client-id` names your own public-client app
   registration if the default one is not allowed), and it refuses to run without a terminal. It reads
   metadata only and is never used by the running application. Data source descriptions drop credentials,
   user info and query strings.

2. **Review and edit** the draft in a pull request. Add `"$schema": "../schemas/contracts.schema.json"` (the
   path relative to your file) and an editor with JSON Schema support completes and checks the file as you type.
   Environments can be declared by workspace id, by name pattern, for one item or for one capacity:

   ```json
   "environments": [
     {"workspace_id": "6f1c…", "environment": "PROD"},
     {"workspace_name_pattern": "Finance *", "environment": "PROD"},
     {"item_id": "9ab2…", "environment": "DEV", "accepts_downgrade": true,
      "note": "experimental notebook inside a PROD workspace"},
     {"capacity_id": "c07e…", "environment": "PROD"}
   ]
   ```

   A reviewed declaration beats the name. Precedence for a workspace is its id, then the first matching pattern
   in file order, then inference from the name; an item takes its workspace's environment unless an `item_id`
   entry says otherwise; a capacity is declared by `capacity_id` and otherwise inferred from its name. The
   same classification reaches workspaces, items, pipeline runs, capacities and the deployment-pipeline stage
   of a declared workspace. Patterns are case-insensitive; `*` matches any run of characters, `?` one
   character, everything else (including `[` and `]`) is literal.

   **Only reviewed entries are enforced.** An entry with `"inferred": true` (a scaffold draft) or with
   environment `UNKNOWN` is *not* applied: the name keeps deciding, so a stale or unfinished draft can never
   pin a workspace that was later renamed. Remove `inferred` once you have confirmed the entry.

   **Lowering production is deliberate and visible.** The environment drives incident severity (PROD is HIGH)
   and policy rules such as `POL-002`, so declaring a workspace the name calls PROD as TEST or DEV lowers its
   incidents' severity and switches its production-only policy rules off. That is allowed (names do lie) but never
   silent: the first time a workspace, item or capacity is classified (at startup, and for any discovered later) every
   override that changes the classification is logged (WARNING for a downgrade) and written
   to the audit trail (`contracts.environment_override`: id, inferred and resolved environment, source, and the
   SHA-256 of the contracts file; plus one `contracts.loaded` record), and `contracts validate` warns about each
   downgrade and about catch-all patterns such as `*`. `"accepts_downgrade": true` records that a reviewer
   confirmed one entry and silences that warning only; the log and audit records remain.

3. **Validate**:

   ```bash
   orqentis contracts validate contracts/estate.json --catalog contracts/catalog.json
   ```

   ```text
   contracts/estate.json:14:7: error: /dependencies/3/target_item_id: '3f9c…' is not a declared source or data product, nor an item in the catalog
   contracts/estate.json:41:9: error: /slas/0/timezone: not a valid IANA time zone (for example Australia/Sydney)
   contracts/estate.json: 2 error(s), 5 warning(s)
   ```

   Errors (non-zero exit): invalid JSON, a field that does not match the schema, duplicate ids or edges, an
   id used by both a source and a data product, an edge from an item to itself, an SLA for an undeclared data
   product or with an invalid time zone or weekdays, and, against a catalog, any reference to a workspace or
   item that does not exist. Warnings: unreviewed (`inferred`) and `UNKNOWN` environment entries (neither is
   enforced), `draft` placeholders, entries that lower a PROD workspace or item (unless `accepts_downgrade`), catch-all
   name patterns, a name pattern matching no workspace, a data product or source whose environment disagrees
   with its workspace's, and references that could not be checked because no catalog was given. Schema errors are reported first; the consistency checks run once the
   schema passes. A full estate file (`fixtures/fabric/estate.json`) is its own catalog.
   `--strict` turns warnings into failures, which is what a CI check on the reviewed file should use.
   The catalog lists workspaces, items and (when the scaffold could read them) capacities; capacity
   declarations are checked only against a catalog that has capacities. Refresh it with
   `scaffold --catalog-output` whenever the estate changes.

4. **Deploy** the file to `ORQENTIS_CONTRACTS_PATH` (for Azure, see the Bicep README, step 4).

The JSON Schema is generated from the model: after changing `EstateContracts` run
`orqentis contracts schema --output schemas/contracts.schema.json`; `orqentis contracts schema --check
schemas/contracts.schema.json` (and a test) fails when the committed file is out of date.

## Delivery pipeline

`ci.yml` runs the gates above plus `actionlint`, `pip-audit`, the Bicep builds and a Trivy scan of the image; `cd.yml`
builds the image once and promotes the same digest `dev` → `test` → `prod`; `release.yml` publishes version tags. Setup,
operation, rollback and how to accept a scan finding are in [delivery.md](delivery.md) (design: ADR-0016). Useful
locally:

```bash
bash scripts/validate-bicep.sh                                   # main.bicep + every .bicepparam, prod writes off
ruff check scripts && ruff format --check scripts                # the delivery scripts
docker run --rm -v "$PWD:/repo" -w /repo rhysd/actionlint:1.7.12 # lint the workflows
```

## Testing

| Suite | Contents |
|---|---|
| `tests/unit` | Dependency traversal and blast radius, classification and taxonomy extension, runbook loading and matching, confidence, policy decisions, approval enforcement, SLA, deployment comparison, audit chain, redaction and untrusted content, tool registry, detection, incident lifecycle |
| `tests/integration` | Storage adapters; REST API; Microsoft Agent Framework investigator with a scripted model (grounding, injection, fallback, budget); Fabric REST adapter against mocked HTTP (auth scopes, retries, pagination, error mapping, status mapping); REST-mode composition end to end; declared environments through severity and policy; `contracts scaffold` against recorded responses (`tests/data/recorded`, replayed by `tests/support/recorded_transport.py`, which refuses every write) |

| `tests/unit` | Delivery pipeline invariants (pinned actions and runners, no secrets, digest promotion, prod writes off) and the dependency-audit gate, dependency traversal and blast radius, classification and taxonomy extension, runbook loading and matching, confidence, policy decisions, approval enforcement, SLA, deployment comparison, audit chain, redaction and untrusted content, tool registry, detection, incident lifecycle |
| `tests/integration` | Storage adapters; REST API; Microsoft Agent Framework investigator with a scripted model (grounding, injection, fallback, budget); Fabric REST adapter against mocked HTTP (auth scopes, retries, pagination, error mapping, status mapping); REST-mode composition end to end |
| `tests/scenarios` | Schema drift, authentication failure, capacity pressure, unknown failure; critical security boundary (`-m security`); CLI demo |

`tests/support/scripted_chat_client.py` plays the model's side of an Agent Framework
conversation, so the real `Agent`, function invocation and telemetry layers run without an
endpoint. `tests/support/fake_entra.py` plays Microsoft Entra ID for real azure-identity
credentials, which is how the tests prove where token requests go and which scope they ask for. Tests clear `ORQENTIS_*` environment variables and ignore `.env`.

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
[security.md](security.md) section 11.

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
