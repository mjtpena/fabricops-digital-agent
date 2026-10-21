---
id: RB-SM-REFRESH-FAILURE-001
version: "1.3.0"
title: Semantic model refresh failure
category: SEMANTIC_MODEL_REFRESH_FAILURE
severity: MEDIUM
signals:
  - "refresh (failed|Failed)"
  - "credentials"
  - "gateway"
  - "out of memory|memory"
  - "timeout"
  - "ModelRefresh_ShortMessage"
  - "cannot access the source"
applies_to: [SemanticModel]
risk: LOW_RISK
approval_required: true
owner: bi-platform@contoso.com
last_reviewed: 2026-10-21
remediation_actions:
  - action_type: REFRESH_SEMANTIC_MODEL
    title: Trigger an on-demand refresh after fixing the cause
    target: failure_point
    idempotent: true
---
# Semantic model refresh failure

## Symptoms
- Scheduled or pipeline-triggered refresh ends in Failed.
- Reports show data from the previous successful refresh.

## Diagnostic Steps
1. Read the refresh error: from the workspace monitoring logs of the model's workspace (`SemanticModelLogs`) or, where the
   deployment can read it, the refresh history (Power BI REST API `refreshes`).
2. Check whether upstream pipelines that feed the model succeeded today.
3. Check data source credentials and gateway status.
4. Check refresh duration and memory against the capacity limits.

## Possible Causes
- Upstream data not ready (pipeline failed or late).
- Data source credentials expired.
- A Direct Lake model points at a table that was renamed, dropped or is not accessible to it ("We cannot access the source
  Delta table"): check the table in the lakehouse and the model's source mapping.
- Gateway offline.
- Model exceeds capacity memory limits.

## Recommended Remediation
1. Fix the upstream cause first (an upstream pipeline incident takes precedence).
2. Trigger an on-demand refresh.

## Validation
- Refresh succeeds and the refresh history shows today's timestamp.
- Key report visuals show today's data.

## Rollback
- None required; a failed refresh leaves the previous data in place.

## Escalation
- Model owner, then BI platform on-call.
