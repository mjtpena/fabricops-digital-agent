---
id: RB-LAKEHOUSE-WRITE-001
version: "1.0.2"
title: Lakehouse (Delta) write failure
category: LAKEHOUSE_WRITE_FAILURE
severity: MEDIUM
signals:
  - "DeltaWriteException"
  - "Concurrent(Append|Modification)Exception"
  - "ProtocolChangedException"
  - "failed to write"
applies_to: [DataPipeline, Notebook, Lakehouse]
risk: LOW_RISK
approval_required: true
owner: platform-reliability@contoso.com
last_reviewed: 2026-07-21
remediation_actions:
  - action_type: RERUN_PIPELINE
    title: Rerun the pipeline once concurrent writers have finished
    target: primary_item
    idempotent: true
---
# Lakehouse (Delta) write failure

## Symptoms
- Notebook or copy activity fails while committing to a Delta table.
- Errors such as `ConcurrentAppendException`, `ConcurrentModificationException` or `ProtocolChangedException`.

## Diagnostic Steps
1. Identify the target Lakehouse table from the error.
2. Run `DESCRIBE HISTORY` on the table and look for overlapping commits from other jobs.
3. Check whether table maintenance (OPTIMIZE / VACUUM) or another pipeline wrote at the same time.

## Possible Causes
- Two jobs writing the same partitions concurrently.
- Table maintenance overlapping the load window.
- Protocol upgrade of the table by another writer.

## Recommended Remediation
1. Wait for the concurrent writer to finish, then rerun the pipeline (the load is idempotent).
2. Separate the schedules of conflicting writers, or partition writes so they do not overlap.

## Validation
- Rerun succeeds and the table history shows a single new commit for the load.

## Rollback
- Use Delta time travel (`RESTORE TABLE ... TO VERSION AS OF`) if a partial write left inconsistent data.

## Escalation
- Owners of the conflicting job.
- Platform on-call if the table protocol changed unexpectedly.
