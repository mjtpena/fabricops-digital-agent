---
id: RB-PIPELINE-TIMEOUT-001
version: "1.0.0"
title: Pipeline or activity timeout
category: PIPELINE_TIMEOUT
severity: MEDIUM
signals:
  - "timed out"
  - "timeout"
  - "exceeded"
applies_to: [DataPipeline, Notebook]
risk: MEDIUM_RISK
approval_required: true
owner: platform-reliability@contoso.com
last_reviewed: 2026-06-02
remediation_actions:
  - action_type: RERUN_PIPELINE
    title: Rerun the pipeline outside the contention window
    target: primary_item
    idempotent: true
  - action_type: MODIFY_CONFIGURATION
    title: Review the activity timeout and degree of parallelism
    target: failure_point
---
# Pipeline or activity timeout

## Symptoms
- Activity fails with "timed out" after running for the configured timeout.
- Run duration is far above the historical median for the pipeline.

## Diagnostic Steps
1. Compare the run duration with the last 10 successful runs.
2. Check capacity utilisation during the run (throttling makes everything slower).
3. Check source system latency and data volume for the run.

## Possible Causes
- Data volume spike.
- Capacity pressure slowing Spark or copy throughput.
- Slow or locked source system.

## Recommended Remediation
1. If capacity or source contention was transient, rerun the pipeline.
2. If volume grew permanently, raise the activity timeout or tune parallelism (configuration change, needs approval in PROD).

## Validation
- Rerun completes within the historical duration range.

## Rollback
- Revert the timeout or parallelism configuration change.

## Escalation
- Source system owner for slow sources.
- Capacity administrators when capacity pressure is the cause.
