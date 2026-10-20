You are implementing **WS7: Signals and detection breadth** in `mjtpena/fabricops-digital-agent`.
Base: latest `origin/main`. Branch: `prod/ws7-signals-detection`. Open one PR to `main` when done; never push to `main`.

## Context

Detection today has one rule (`PipelineFailureRule`) and one workflow (`PipelineFailureWorkflow`). Rest mode collects
pipeline job instances, activity runs and semantic-model refreshes, but refreshes never open incidents, notebook /
Spark job definition / Dataflow jobs are not collected, schema-change events have no producer, and capacity CU
utilisation is unavailable. Decisions: [ADR-0009](../../adr/0009-capacity-and-schema-signals.md) (capacity from the
Capacity Metrics semantic model via `executeQueries`, confirmed by a spike; schema drift from Delta `_delta_log` over
OneLake for contract-declared tables) and **D9** (semantic-model refresh failures: the reader never gets write; read them from Fabric **workspace monitoring** (semantic-model logs in the monitoring Eventhouse, read with workspace Viewer), validated by a spike in this workstream; pipeline-triggered refreshes also surface as pipeline activity failures; the read-only admin API is a fallback only with the owner's further approval).

Live findings from WS5 (see `docs/security.md` and `infra/bicep/README.md` §5, `docs/live-environment.md`):
* workspace **Viewer** reads workspaces, items, job instances, activity runs; job instances come back **newest first**;
* OneLake `_delta_log` needs a OneLake data-access role with **Read on the specific table** (Viewer alone is 403);
* semantic-model **refresh history needs dataset Write** (403 for Viewer; dataset Read/Build cannot be granted to service
  principals; Fabric job-instances returns 404 for semantic models), but `GET .../refreshes/{requestId}` works with Viewer;
* sandbox injectors exist for schema drift, notebook failure, pipeline timeout, auth failure, refresh failure and SLA
  breach (`scripts/live-estate/inject/`), with recorded, sanitised fixtures under `tests/fixtures/fabric_rest/`;
* follow-ups found live: no error signature for generic notebook exceptions (category UNKNOWN), schema-drift column
  extraction shows `'unknown'`, and redaction over-matches `signature_match`.

Wave 1/2 runtime you build on: scheduler and SLA sweep (SLA assessments are recorded as operational events for a rule
to consume), per-scope watermarks, detection cursor (ADR-0013); declared environments (ADR-0014).

## Goal

Each injected failure class is detected by a deterministic rule within one collection interval and investigated by a
workflow that reaches the right root-cause category (or an honest LOW-confidence "undetermined"), using only read-only
access with proven minimum grants.

## In scope

1. **Collectors**: Fabric job instances for `Notebook`, `SparkJobDefinition` and `Dataflow` items (reuse the
   per-scope watermarks and the newest-first early stop); normalised `OperationalEvent`s.
2. **Detection rules** (deterministic, de-duplicated like the pipeline rule): notebook job failure; Spark job definition
   failure; Dataflow refresh failure; **semantic-model refresh failure** (source per D9); **SLA breach** (consumes the SLA
   sweep's events; severity from the declared environment and data product).
3. **Investigation workflows** for refresh failure, SLA breach and notebook/Spark failure, reusing the tool registry,
   evidence recorder, grounding validator, confidence calculator, runbooks, policy and reports (read
   `incidents/workflows/pipeline_failure.py` for the pattern; share steps rather than copy them). Add runbooks where
   missing (notebook failure, SLA breach), validated by `orqentis runbooks`.
4. **Schema-change collector**: for tables declared in contracts, read `_delta_log` JSON commits through the OneLake
   ADLS Gen2 API (read-only; Entra token for `https://storage.azure.com/.default` or the OneLake scope), detect column
   adds/removes/renames/type changes between versions, emit `SCHEMA_CHANGED` events with before/after columns. Treat
   every value as untrusted (sanitise, envelope, length limits). Fix the column extraction so the report names the
   changed columns.
5. **Capacity collector** (rest mode): a spike first. Document the Capacity Metrics semantic model's tables and the DAX
   needed for CU % and throttling per timepoint, from Microsoft documentation and the app's published model; implement
   an `executeQueries` adapter behind the `CapacityService` port with recorded fixtures. Live validation needs the owner
   to approve read access to the Capacity Metrics app workspace (ledger R4); build and test it against fixtures.
6. **Signatures and redaction follow-ups**: an error signature for generic notebook exceptions (Python/Spark traceback
   shapes) mapped to a sensible category; fix the `signature_match` redaction over-match without weakening redaction.
7. Optional: deployment-drift detection only if a read-only path exists with the reader's grants; otherwise document
   why not.
8. Docs: `docs/architecture.md` (ports, collectors, rules), `docs/security.md` (new untrusted inputs: Delta logs,
   capacity data; minimum grants), `infra/bicep/README.md` §5 grants, an ADR for the new detection set and signal sources.

## Out of scope

Write actions, notifications, storage engines, alerts. Any live tenant access (the orchestrator runs the live game day).

## Acceptance criteria

1. Unit tests for each collector, rule and workflow; scenario tests (new fixtures under `fixtures/telemetry/` and
   `tests/fixtures/fabric_rest/`) for notebook failure, refresh failure, SLA breach and schema drift with the
   expected category, runbook and confidence band; the four existing scenarios unchanged.
2. Prompt-injection tests: a poisoned column name / Delta commit info / capacity label is sanitised and flagged, and
   cannot change policy, approvals or grounding.
3. `make check`, `make demo`, `make demo-all`, `pytest -m security`, `docker build`, `az bicep build` pass.
4. Live (done by the orchestrator after merge): each WS5 injector yields the correct category.
