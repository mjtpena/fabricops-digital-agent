# ADR-0006: Policy engine, approval binding and separate read/write capabilities

* Status: Accepted (Milestone 6)
* Date: 2026-10-09

## Context

The MVP is read-only by default. Mutations must pass an explicit action/approval boundary that
the agent cannot override, and READ and WRITE must be separate capability boundaries.

## Decision

* `FabricClient` (read) and `FabricWriteClient` (write) are separate ports. Only the
  `ActionGateway` receives a write client, and the default is `DisabledFabricWriteClient`.
* `PolicyEngine` evaluates a YAML policy in code. Precedence is DENY > REQUIRE_APPROVAL > ALLOW >
  default DENY; unclassified action types count as HIGH_RISK. A code invariant makes every
  mutation require approval in the MVP, whatever the policy says. The policy also holds the
  agent's tool allowlist.
* Mutation tools only submit to the gateway and return `ACTION_REQUIRES_APPROVAL` or
  `ACTION_DENIED`.
* An `ApprovalRequest` embeds the proposed action and a SHA-256 fingerprint of what would execute.
  Only a human with the required role who is not the requester may decide it, before it expires.
* Execution is a separate, explicit step that re-checks every condition: approval state, expiry,
  fingerprint, role, single use, current policy and write capability.
* The `ApprovalProvider` port lets Teams (milestone 10) replace the API-driven local provider.

## Consequences

* An agent mistake, or a prompt injection, cannot cause a change.
* Enabling real writes needs a reviewed REST write adapter and the actor identity. This is
  deliberately not part of the MVP.
