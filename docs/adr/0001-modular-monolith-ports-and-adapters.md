# ADR-0001: Modular monolith with ports and adapters

* Status: Accepted (Milestone 1)
* Date: 2026-10-09

## Context

The MVP must run fully offline (`make demo`, tests). It must then connect to live Fabric, Azure
OpenAI, Eventhouse, OneLake and Teams without rewriting business logic. The specification asks
for no premature microservices.

## Decision

* One deployable Python package (`orqentis`) with packages per capability.
* Every external system is a `typing.Protocol` port (`FabricClient`, `FabricWriteClient`,
  `EventStore`, `EvidenceStore`, `IncidentRepository`, `ApprovalProvider`, `AuditTrail`,
  `Reasoner`, `TokenProvider`, ...).
* Adapters live next to their port or in `storage/`. `bootstrap.py` is the single composition
  root that chooses adapters from `Settings`.
* Mocks exist for every Microsoft dependency: a synthetic Fabric estate, a scripted Agent
  Framework chat client, and the local stores.

## Consequences

* Domain and application code are tested without network access, and swapping adapters is a
  `bootstrap.py` change.
* The single process with SQLite stores constrains the MVP to one replica (see ADR-0003).
