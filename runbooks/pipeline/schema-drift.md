---
id: RB-SCHEMA-DRIFT-001
version: "1.3.0"
title: Upstream schema drift breaks a pipeline transformation
category: SCHEMA_DRIFT
severity: HIGH
signals:
  - "column .{0,40}(is missing|not found|does not exist)"
  - "UNRESOLVED_COLUMN"
  - "cannot resolve"
  - "schema (change|mismatch)"
  - "column \\w+ removed"
applies_to: [DataPipeline, Notebook, Lakehouse]
risk: MEDIUM_RISK
approval_required: true
owner: platform-reliability@contoso.com
last_reviewed: 2026-09-15
remediation_actions:
  - action_type: MODIFY_ITEM_DEFINITION
    title: Update the notebook schema mapping for the changed column
    target: failure_point
    parameters:
      change_path: "Git (DEV) -> deployment pipeline -> TEST -> PROD"
  - action_type: RERUN_PIPELINE
    title: Rerun the pipeline after the mapping change is deployed
    target: primary_item
    idempotent: true
---
# Upstream schema drift breaks a pipeline transformation

## Symptoms
- A pipeline activity (usually a notebook) fails with `UNRESOLVED_COLUMN`, `cannot resolve`, or "expected column ... is missing".
- The upstream copy activity succeeded; the failure is in the first transformation that reads the changed table.
- A Delta schema change (new table version) is recorded on the upstream Lakehouse table shortly before the failure.

## Diagnostic Steps
1. Identify the missing column and the table it was expected in from the error message.
2. Compare the current Delta schema of the upstream table with the previous version (`DESCRIBE HISTORY`, or the schema-watcher event).
3. Confirm the change originated upstream (source release notes, source owner), not in the notebook: check the deployment history of the failing item.
4. Decide whether the column was renamed (one removed, one added with a compatible type) or dropped.
5. List the downstream data products and SLAs that depend on the failing transformation.

## Possible Causes
- A source system release renamed or removed a column.
- The copy activity mapping was switched to auto-map and picked up the new source schema.
- A notebook change introduced a new column expectation (rule out with deployment history).

## Recommended Remediation
1. Agree the change with the source owner. If the rename is intended, update the notebook schema mapping (for example map `client_id` to `customer_id`) in DEV through Git.
2. Promote the change through the deployment pipeline (DEV, TEST, PROD). Do not hot-edit PROD.
3. Rerun the pipeline in PROD after the deployment completes.
4. Notify the owners of affected data products if the SLA will be missed.

## Validation
- The pipeline run completes successfully.
- Row counts in the gold table are within 5% of the previous day.
- The downstream semantic model refresh succeeds and dependent reports render.

## Rollback
- Revert the mapping commit and redeploy the previous notebook version.
- If the source can roll back its release, rerun with the original schema.

## Escalation
- Source system owner for the upstream change.
- Data product owner when the SLA will be breached.
- Platform on-call when the change affects more than one pipeline.
