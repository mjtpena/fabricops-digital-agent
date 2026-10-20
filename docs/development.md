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
   silent: at startup every override that changes a classification is logged (WARNING for a downgrade) and written
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
   Refresh the catalog with `scaffold --catalog-output` whenever the estate changes.

4. **Deploy** the file to `ORQENTIS_CONTRACTS_PATH` (for Azure, see the Bicep README, step 4).

The JSON Schema is generated from the model: after changing `EstateContracts` run
`orqentis contracts schema --output schemas/contracts.schema.json`; `orqentis contracts schema --check
schemas/contracts.schema.json` (and a test) fails when the committed file is out of date.

## Testing

| Suite | Contents |
|---|---|
| `tests/unit` | Dependency traversal and blast radius, classification and taxonomy extension, runbook loading and matching, confidence, policy decisions, approval enforcement, SLA, deployment comparison, audit chain, redaction and untrusted content, tool registry, detection, incident lifecycle |
| `tests/integration` | Storage adapters; REST API; Microsoft Agent Framework investigator with a scripted model (grounding, injection, fallback, budget); Fabric REST adapter against mocked HTTP (auth scopes, retries, pagination, error mapping, status mapping); REST-mode composition end to end; declared environments through severity and policy; `contracts scaffold` against recorded responses (`tests/data/recorded`, replayed by `tests/support/recorded_transport.py`, which refuses every write) |
| `tests/scenarios` | Schema drift, authentication failure, capacity pressure, unknown failure; critical security boundary (`-m security`); CLI demo |

`tests/support/scripted_chat_client.py` plays the model's side of an Agent Framework
conversation, so the real `Agent`, function invocation and telemetry layers run without an
endpoint. `tests/support/fake_entra.py` plays Microsoft Entra ID for real azure-identity
credentials, which is how the tests prove where token requests go and which scope they ask for. Tests clear `ORQENTIS_*` environment variables and ignore `.env`.

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
