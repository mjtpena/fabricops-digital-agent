You are implementing **WS2: Portability and contracts** in `mjtpena/fabricops-digital-agent`.
Base: latest `origin/main`. Branch: `prod/ws2-portability-contracts`. Open one PR to `main` when done; never push to `main`.

## Context

FabricOps must be deployable by someone else, into a fresh resource group and a different Fabric
tenant, using only the docs, a parameter file and a contracts scaffolding tool. Today:

* PROD/TEST/DEV is inferred from workspace names (`infer_environment` in
  `src/orqentis/fabric/rest/client.py`, used for workspaces and items). The environment drives
  incident severity and policy rule `POL-002` (`src/orqentis/policies/default_policy.yaml`), and
  there is no override.
* The contracts file (`EstateContracts` in `src/orqentis/fabric/fixtures.py`: external sources,
  data products, dependencies, SLAs; loaded by `load_contracts`) must be written by hand. Rest mode
  requires `ORQENTIS_CONTRACTS_PATH`; a missing or invalid file makes startup fail, which crash-loops
  the Container App.
* Token scopes are hard-coded to the public cloud (`FABRIC_SCOPE`, `POWERBI_SCOPE` in
  `src/orqentis/fabric/rest/auth.py`); the Fabric and Power BI base URLs are already settings.
* `default_timezone` defaults to `Australia/Sydney` and is not exposed in Bicep.
* A managed identity cannot reach a Fabric tenant in another Entra tenant. `AuthMode.SERVICE_PRINCIPAL`
  uses `EnvironmentCredential`, but Bicep has no Key Vault wiring for its secret or certificate.
* `target_item_id(activity_input)` in the REST client already extracts the Fabric item an activity
  acts on, which is the raw material for drafting `TRIGGERS` edges.

## Goal

Make environment classification declarable, contracts authorable and validatable with tools,
startup tolerant of missing or invalid contracts, and the identity and cloud endpoints configurable
for other tenants and sovereign clouds.

## In scope

1. **Declared environment overrides** in the contracts file: a new optional section (for example
   `environments: [{workspace_id | workspace_name_pattern, environment}]`) that takes precedence
   over name inference. Put the resolver in its own module (for example
   `src/orqentis/topology/environment.py`) and change only the call sites in the REST client.
   Severity and policy must use the resolved environment. Items inherit their workspace's resolved
   environment unless declared otherwise. Mock fixtures keep working unchanged.
2. **JSON Schema for contracts** generated from the Pydantic model (committed, for example
   `schemas/contracts.schema.json`, plus a test that fails if the committed schema drifts from the
   model).
3. **`orqentis contracts validate <path>`**: schema validation plus referential checks (dependency
   endpoints exist among declared sources or items when a catalog is available; SLA targets exist;
   no duplicate ids). Clear, line-oriented error messages; non-zero exit on errors.
4. **`orqentis contracts scaffold`**: drafts a contracts file from a live listing through the
   existing read-only `FabricClient` port: workspaces (with an `environments` section pre-filled
   from name inference, marked as inferred), items, `TRIGGERS` edges from observed pipeline
   activity runs (`target_item_id`), and placeholders for external sources, data products and SLAs.
   Optional `--scanner` mode uses the Power BI admin Scanner API (`/admin/workspaces/getInfo` →
   `scanStatus` → `scanResult`) to add lineage, only when run interactively by a Fabric admin; it
   must stay read-only and must not be used by the app runtime. Output is a draft for human
   review, never applied automatically. The scaffold must run against **recorded fixtures**
   (mocked `httpx` transports), since you have no tenant access; add those fixtures.
5. **No crash loop on missing or invalid contracts.** Contract loading raises a clear, typed error
   naming the path and every violation. (WS1 makes the app stay up with readiness 503 on any
   container-build failure; you provide the clear error and the CLI.) The app must not log the
   contracts content beyond what is needed.
6. **Sovereign clouds.** Settings for the Entra authority host and the Fabric and Power BI token
   scopes (defaults: public cloud). Pass the authority host to every `azure-identity` credential the
   app builds. Unit tests prove US Government (or another sovereign profile) values flow to the
   credential and the scopes. Document the known endpoint sets (public, US Gov, China) with a note
   that Fabric availability differs by cloud; a live sovereign test is not required.
7. **Time zone in Bicep.** Expose `ORQENTIS_DEFAULT_TIMEZONE` as a Bicep parameter (`defaultTimezone`,
   default `Australia/Sydney` for backwards compatibility) and validate it at startup.
8. **Service-principal mode with Key Vault** for a Fabric tenant different from the Azure tenant:
   Bicep parameters to select `service_principal` mode, the Fabric tenant id, the client id and a
   Key Vault secret reference (Container Apps `keyVaultUrl` + identity) for the client secret, or a
   certificate. No secret values in parameters or the repo. Document the cross-tenant setup in
   `infra/bicep/README.md`.
9. Docs: `docs/development.md` (contracts workflow), `infra/bicep/README.md`, `README.md` (brief),
   `.env.example`. ADR for declared environment classification and the contracts schema.

## Out of scope

Scheduler, readiness endpoints and catalog refresh (WS1). Auth of API callers (WS3). CI/CD (WS4).
Collectors and detection (WS7). Any live tenant access.

## Coordination with parallel workstreams

* WS1 edits `list_job_instances` and the HTTP layer of `fabric/rest/client.py` and owns
  `api/app.py` lifespan and health routes. Do not restructure those; touch only the environment
  call sites in the client.
* WS3 edits `api/auth.py` and Easy Auth Bicep. Keep your Bicep changes to new parameters, new
  environment variables and the Key Vault secret reference.

## Likely files

`src/orqentis/topology/environment.py` (new), `src/orqentis/fabric/fixtures.py`,
`src/orqentis/fabric/rest/client.py` (call sites only), `src/orqentis/fabric/rest/auth.py`,
`src/orqentis/contracts/` (new: schema, validation, scaffold), `src/orqentis/cli.py`,
`schemas/contracts.schema.json`, tests and fixtures under `tests/` and `fixtures/`. Hot files you
may touch additively: `bootstrap.py`, `settings.py`, `.env.example`, `infra/bicep/main.bicep`,
`README.md`, `docs/architecture.md`, `docs/security.md`.

## Acceptance criteria

1. Tests: a declared override beats name inference (by id and by pattern); an undeclared workspace
   falls back to inference; severity and `POL-002` follow the resolved environment.
2. Tests: the committed JSON Schema matches the model; `contracts validate` passes the example file
   and reports each class of error with a non-zero exit.
3. Test: `contracts scaffold` against recorded fixtures produces a file that passes
   `contracts validate`, includes inferred environments and `TRIGGERS` edges from activity inputs,
   and makes no write calls (the transport rejects non-GET requests other than the Scanner POSTs in
   `--scanner` mode).
4. Test: a missing or invalid contracts file raises the typed error with the path and violations.
5. Tests: authority host and scopes are configurable and reach the credential and the token
   requests; defaults are unchanged.
6. `az bicep build` / `build-params` pass with the new parameters; the README documents
   cross-tenant service-principal mode with Key Vault.
7. `make check`, `make demo`, `make demo-all`, `docker build` pass; `pytest -m security` passes.
