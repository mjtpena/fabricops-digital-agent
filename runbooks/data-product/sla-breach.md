---
id: RB-SLA-BREACH-001
version: "1.0.0"
title: Data product SLA breached
category: SLA_BREACH
severity: HIGH
signals:
  - "SLA breach"
  - "late"
  - "blocked"
  - "not run"
applies_to: [DataPipeline, Notebook, SparkJobDefinition, Dataflow, SemanticModel]
risk: READ_ONLY
approval_required: false
owner: data-platform@contoso.com
last_reviewed: 2026-10-21
remediation_actions:
  - action_type: MANUAL_STEP
    title: Tell the data product owner and its consumers that the SLA is breached and when the data is expected
    target: primary_item
---
# Data product SLA breached

A data product's refresh did not finish by its declared deadline. The breach is recorded by the SLA sweep from freshness
events, so it is a fact even when no error occurred. The investigation says *why*: the refresh ran late, something upstream
blocked it, its last run failed, or it did not run.

## Symptoms
- The SLA of a data product is `BREACHED`: the deadline passed without a successful refresh of the item that produces it.
- Reports that consume the product show yesterday's data.

## Diagnostic Steps
1. Read the breach reason: refreshed late (and when), not refreshed since, or no refresh observed.
2. Find the incident that blocks the refresh, if any: an open incident on the producing item or on anything upstream of it.
3. If it ran late, compare its start time and duration with its earlier runs (a late trigger, a slower run, a queue).
4. If it did not run at all, check the trigger: the schedule, the pipeline that should start it, a disabled item.
5. List the downstream data products and reports and which of them have their own SLAs.

## Possible Causes
- An upstream failure (see the blocking incident): fix that first.
- A late trigger: schedule changed, disabled or delayed by a dependency.
- A slow run: data volume, capacity contention, a source system slow to respond.
- The refresh was never started (trigger removed in a deployment).

## Recommended Remediation
1. Resolve the blocking incident, or rerun the producing item if it only failed transiently.
2. Tell the owner of the data product and its consumers that the SLA is breached, with the expected time of recovery.
3. If the run was late, record why and decide whether the deadline, the schedule or the capacity needs to change.

## Validation
- The producing item completes and the data product's last successful update is current.
- Downstream semantic models have refreshed after it.

## Rollback
- Not applicable.

## Escalation
- The data product owner.
- Platform on-call when several products are breached at once.
