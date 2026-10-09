---
id: RB-CAPACITY-THROTTLING-001
version: "2.0.0"
title: Capacity throttling causes workload failures
category: CAPACITY_PRESSURE
severity: HIGH
signals:
  - "CapacityLimitExceeded"
  - "TooManyRequestsForCapacity"
  - "throttl"
  - "exceeded its limits"
  - "background (operations are being )?reject"
applies_to: [DataPipeline, Notebook, SemanticModel]
risk: MEDIUM_RISK
approval_required: true
owner: capacity-admins@contoso.com
last_reviewed: 2026-09-30
remediation_actions:
  - action_type: RERUN_PIPELINE
    title: Rerun the failed pipeline once capacity utilisation is back below 80%
    target: primary_item
    idempotent: true
  - action_type: MODIFY_CONFIGURATION
    title: Reschedule or move the contending workload off the production capacity
    target: contending_workload
---
# Capacity throttling causes workload failures

## Symptoms
- Spark sessions fail to start, or jobs fail with `CapacityLimitExceeded` / HTTP 430.
- Capacity Metrics show utilisation above 100% with interactive delay or background rejection.
- Failures cluster at the same time of day, often on the same weekday.

## Diagnostic Steps
1. Get capacity utilisation, interactive delay and background rejection for the failure window.
2. Identify the top consuming workloads in that window, including workloads from other workspaces on the same capacity.
3. Check whether a non-production workspace is assigned to the production capacity.
4. Look for a recurring pattern (same weekday and time window) in previous incidents.

## Possible Causes
- A heavy batch job (backfill, ML training) overlapping the production load window.
- A non-production workspace assigned to the production capacity.
- Organic growth beyond the capacity SKU.

## Recommended Remediation
1. Rerun the failed pipeline once utilisation is back below 80%.
2. Reschedule the contending workload outside the production window, or move its workspace to a non-production capacity.
3. If contention is organic growth, open a capacity review. Scaling the SKU is a HIGH risk change that needs capacity administrator approval.

## Validation
- Rerun completes; utilisation stays below 100% with no background rejection during the next load window.
- The next scheduled run on the recurring weekday succeeds.

## Rollback
- Restore the previous schedule or workspace capacity assignment.

## Escalation
- Capacity administrators.
- Owner of the contending workload.
