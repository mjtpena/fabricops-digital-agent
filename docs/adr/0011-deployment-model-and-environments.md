# ADR-0011: Single-tenant deployment model and environment topology

* Status: Accepted (programme decision D4)
* Date: 2026-10-20

## Context

FabricOps reads a Fabric estate with a least-privilege identity and can, under approval, change it
with a separate actor identity. Multi-tenant SaaS would concentrate credentials for many customers'
estates in one service and complicates data residency and isolation.

## Decision

* **Single-tenant:** one deployment per customer tenant and subscription. Multi-tenant SaaS is out
  of scope.
* Reference topology (and this programme's deployment): resource groups `rg-fabricops-dev`,
  `rg-fabricops-test` and `rg-fabricops-prod`, plus `rg-fabricops-shared` holding one Azure
  Container Registry (and, later, the Azure OpenAI account). Images are built once and promoted
  between environments by digest.
* Each environment has its own managed identities, storage and configuration. GitHub Actions
  deploys over OIDC federated credentials, with a separate Entra identity per environment so that
  the `dev` pipeline cannot change `prod`.
* Region, names and the Fabric tenant are parameters, so another customer deploys the same
  template into their own subscription and tenant.

## Consequences

* No shared control plane and no cross-customer data path.
* Portability is a deliverable: docs, parameter files and the contracts scaffold must be enough to
  deploy into a fresh resource group and a different Fabric tenant.
