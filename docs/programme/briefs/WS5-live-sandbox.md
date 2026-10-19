# WS5: Live sandbox estate and live test harness (orchestrator-run)

Runs in the orchestrator session (or a local subagent sharing its `az` login), because it needs the
live tenant. Branch: `prod/ws5-live-sandbox`. One PR to `main`.

## Preconditions

Owner approval of the Wave 1 list: A1 (sandbox resource group), A2 (F2 capacity), A3 (sandbox
workspaces and items), A4 (least-privilege test service principal), A5 (budget). Decision D0.

## Plan

1. `scripts/live-estate/` with idempotent `create.sh` and `teardown.sh` (bash + `az rest`), plus a
   `config.env.example` (names and ids only, no secrets). Create, in order:
   * workspaces `FabricOps Sandbox [DEV]`, `FabricOps Sandbox [TEST]`, `FabricOps Sandbox [PROD]`
     and `FabricOps Sandbox Consumer` on the sandbox capacity;
   * in each environment workspace: lakehouse `crm_source` with table `customers`, notebook
     `transform_customers`, pipeline `ingest_customers` running the notebook;
   * in the consumer workspace: a semantic model over the transformed table and a report;
   * a matching contracts file (`scripts/live-estate/contracts.sandbox.json`), validated with
     `orqentis contracts validate` once WS2 lands.
2. Fault injectors (`scripts/live-estate/inject/*.sh`), each reversible:
   schema drift (rename a source column); notebook failure (a notebook that raises); pipeline
   timeout (an activity with a short timeout); auth-style failure (a connection with a deliberately
   invalid, fake credential, never a real one); semantic-model refresh failure; SLA breach (a
   declared SLA the pipeline cannot meet).
3. `tests/live/` with the `live` pytest marker (registered in `pyproject.toml`), skipped unless
   `ORQENTIS_LIVE_TESTS=1`. Runs the REST adapter as the least-privilege test service principal and
   asserts the mappings (workspaces, items, job instances, activity runs, refreshes, errors).
4. Least-privilege discovery: prove workspace Viewer is enough for each read; find and prove the
   minimum grant for OneLake `_delta_log` reads (expected: item-level Read + ReadAll on the source
   lakehouse, no write). Stop and ask the owner if the minimum grant includes write rights.
   Document each finding in `docs/security.md` and `infra/bicep/README.md`.
5. Record sanitised real responses as fixtures (ids remapped, names generic, no tenant data, no
   secrets) and use them to harden the mocked REST tests. Every mapping mismatch gets an adapter fix
   and a regression test.
6. Log every tenant change in `docs/live-environment.md`, keep `teardown.sh` current, pause the
   capacity when idle.

## Acceptance

Create, inject, collect, detect and teardown all work live; every mapping mismatch found is fixed
with a regression test; least-privilege grants documented with evidence.
