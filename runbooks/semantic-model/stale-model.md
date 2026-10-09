---
id: RB-SM-STALE-001
version: "1.0.1"
title: Stale semantic model or data product
category: DATA_STALENESS
severity: MEDIUM
signals:
  - "stale"
  - "not refreshed"
  - "freshness"
  - "SLA"
applies_to: [SemanticModel, DataProduct]
risk: LOW_RISK
approval_required: true
owner: bi-platform@contoso.com
last_reviewed: 2026-09-01
remediation_actions:
  - action_type: REFRESH_SEMANTIC_MODEL
    title: Refresh the stale semantic model once upstream data is current
    target: affected_semantic_models
    idempotent: true
---
# Stale semantic model or data product

## Symptoms
- The data product has not been refreshed within its SLA window.
- No refresh failure is recorded; the refresh never ran (skipped or not triggered).

## Diagnostic Steps
1. Find the last successful refresh and the item that is supposed to trigger it.
2. Check whether the triggering pipeline ran, failed, or skipped the refresh activity.
3. Check the SLA deadline and the minutes remaining.

## Possible Causes
- Upstream pipeline failed and skipped the refresh activity.
- Schedule disabled or changed.
- Refresh trigger removed in a deployment.

## Recommended Remediation
1. Resolve the upstream failure.
2. Trigger a refresh of the stale model.
3. Tell data product consumers if the SLA is breached.

## Validation
- Last refresh time is within the SLA window.

## Rollback
- Not applicable.

## Escalation
- Data product owner.
