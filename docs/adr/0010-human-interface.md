# ADR-0010: Human interface: notifier port and a minimal server-rendered UI

* Status: Accepted (programme decision D3)
* Date: 2026-10-20

## Context

FabricOps has no notifications and no UI. Approvals mean raw API calls with
`az account get-access-token`. Autonomous operation needs a human to learn about an incident within
minutes and to approve or reject recommended actions without tooling.

## Decision

* A **`Notifier` port** with two adapters: a Microsoft Teams **Workflows webhook** (Power Automate
  "When a Teams webhook request is received") and a **generic webhook**. Webhook URLs are secrets
  held in Key Vault or as Container App secrets, never in the repo or plain settings. Notifications
  go out when an incident is opened or escalated, an approval is requested and an action is
  executed, with deep links to the UI. Content is redacted before sending.
* A **minimal server-rendered web UI** behind Easy Auth (Entra ID): incident list, the report with
  FACT / INFERENCE / RECOMMENDATION labels, approvals with approve / reject / execute and comments,
  and audit verification. It calls the same services as the API and inherits every policy and
  approval check; it holds no extra privilege.
* **Teams Adaptive Card approvals are deferred.** Approving inside Teams would add a second
  approval surface that must re-implement the human, role, fingerprint and expiry checks.

## Consequences

* The Easy Auth configuration must support interactive browser sign-in. Prefer a federated or
  managed-identity client credential over a client secret where Container Apps supports it.
* Human principals arrive both from bearer access tokens (API) and from ID-token sessions (UI); the
  principal classification (WS3) must treat both correctly and fail closed.
