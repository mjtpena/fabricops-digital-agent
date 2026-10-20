# Architecture Decision Records

| ADR | Decision | Status |
|---|---|---|
| [0001](0001-modular-monolith-ports-and-adapters.md) | Modular monolith with ports and adapters | Accepted |
| [0002](0002-deterministic-orchestration-around-the-agent.md) | Deterministic orchestration around a Microsoft Agent Framework agent | Accepted |
| [0003](0003-local-stores-first.md) | Local development stores first; Eventhouse and OneLake behind the same ports | Accepted |
| [0004](0004-relational-dependency-graph.md) | Relational dependency graph; declared lineage | Accepted |
| [0005](0005-deterministic-confidence.md) | Deterministic, explainable confidence | Accepted |
| [0006](0006-policy-approval-and-capability-boundaries.md) | Policy engine, approval binding and separate read/write capabilities | Accepted |
| [0007](0007-grounding-validation.md) | Accept agent output only when grounded in evidence | Accepted |
| [0008](0008-production-storage.md) | Production storage: PostgreSQL system of record, immutable Blob evidence (supersedes ADR-0003's open question) | Accepted |
| [0009](0009-capacity-and-schema-signals.md) | Capacity and schema-change signals in rest mode | Accepted |
| [0010](0010-human-interface.md) | Human interface: notifier port and a minimal server-rendered UI | Accepted |
| [0011](0011-deployment-model-and-environments.md) | Single-tenant deployment model and environment topology | Accepted |
| [0012](0012-llm-hosting.md) | LLM hosting for the Agent Framework reasoner | Accepted |
| [0013](0013-scheduler-lease-and-async-investigation-jobs.md) | In-process scheduler behind a leader lease, asynchronous investigation jobs, durable detection, incremental collection | Accepted |
| [0014](0014-declared-environments-and-contracts-schema.md) | Declared environment classification, the contracts schema and portable identity | Accepted |
| [0015](0015-principal-classification-and-api-hardening.md) | Principal classification from token claims, Automation role and API hardening | Accepted |
| [0016](0016-delivery-pipeline.md) | Delivery pipeline: build once, scan, promote by digest | Accepted |
