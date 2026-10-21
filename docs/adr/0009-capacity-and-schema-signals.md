# ADR-0009: Capacity and schema-change signals in rest mode

* Status: Accepted (programme decision D2); the capacity source is confirmed by a WS7 spike
* Date: 2026-10-20

## Context

In rest mode two signals that the investigation relies on have no producer. Capacity CU
utilisation has no public REST API (the capacity tool reports `CAPABILITY_UNAVAILABLE`). Delta
schema changes are replayed only from fixtures in mock mode. Both are central to the schema-drift
and capacity-pressure scenarios.

Options for capacity: the Fabric Capacity Metrics app's semantic model queried through the
Power BI `executeQueries` REST API; Real-Time hub capacity events streamed into an Eventhouse; or
workspace monitoring (which covers item-level logs, not capacity CU).

## Decision

* **Capacity:** read capacity utilisation from the **Capacity Metrics semantic model via
  `executeQueries`**, through a dedicated read-only adapter. This needs no new Fabric items, and the
  tenant setting *Semantic Model Execute Queries REST API* is already on. A WS7 spike validates the
  model's tables and freshness on the sandbox capacity before the adapter is built. If the model's
  schema proves too unstable across app versions, fall back to Real-Time hub capacity events in an
  Eventhouse.
* **Schema drift:** detect schema changes by reading the Delta `_delta_log` of tables **declared
  in the contracts file**, through the OneLake ADLS API, with the minimum read grant proven live
  (expected: item-level Read + ReadAll on the source lakehouse; workspace Viewer alone does not
  grant OneLake file reads). If the minimum grant includes any write right, the decision returns to
  the owner.
* Both collectors are deterministic, emit `OperationalEvent`s, and treat everything they read as
  untrusted (sanitisation, envelope, redaction), like every other Fabric signal.

## Consequences

* The reader identity needs read access to the Capacity Metrics app's workspace and item-level
  OneLake read on declared source lakehouses; both are documented as least-privilege grants.
* Capacity signals include load from every workspace on the capacity, not only monitored ones.
* Delta log content (column names, operation metadata) is a new prompt-injection surface and is
  covered by the security review (WS12).

## Update (ADR-0020, WS7)

* **Schema drift** is implemented as decided: the Delta `_delta_log` of tables declared in the contracts (`delta_tables`), read
  through the OneLake ADLS Gen2 API with a table-level OneLake data access role (smaller than the expected `Read` + `ReadAll`,
  proven live; see `docs/security.md` section 11).
* **Capacity** is built (`CapacityMetricsReader`, `executeQueries`) but the spike could **not confirm** the source, offline: the
  model is documented as unsupported outside the app's own reports, two app versions already name their tables differently,
  and `executeQueries` needs Build on the model, which a Viewer does not have and which cannot be granted to a service
  principal through the API. The adapter fails closed on a schema mismatch and stays dormant until configured. The fallback
  stated above (Real-Time hub capacity events in an Eventhouse) remains the documented alternative; the choice is the owner's.

