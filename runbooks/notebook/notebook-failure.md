---
id: RB-NOTEBOOK-FAILURE-001
version: "1.0.0"
title: Notebook, Spark job or Dataflow raised an exception
category: ACTIVITY_FAILURE
severity: MEDIUM
signals:
  - "Traceback \\(most recent call last\\)"
  - "notebook execution (is in )?failed"
  - "\\b[A-Z][A-Za-z0-9_]*(Error|Exception)\\b"
  - "SparkException|Py4JJavaError|PythonException"
applies_to: [Notebook, SparkJobDefinition, Dataflow, DataPipeline]
risk: READ_ONLY
approval_required: false
owner: platform-reliability@contoso.com
last_reviewed: 2026-10-21
remediation_actions:
  - action_type: INSPECT_LOGS
    title: Read the run's traceback and driver log in the Monitoring hub
    target: failure_point
    idempotent: true
  - action_type: MANUAL_STEP
    title: Fix the code or the data the exception points at, then promote the fix through the deployment pipeline
    target: failure_point
---
# Notebook, Spark job or Dataflow raised an exception

This runbook covers a run that failed because its **own code or data** raised: a Python or Spark exception, with no
platform signal (authentication, capacity, timeout, missing column) that explains it better. Those have their own runbooks
and take precedence when their signature matches.

## Symptoms
- A notebook, Spark job definition or Dataflow run ends in `Failed`; a pipeline running it reports the same error.
- The error carries an exception name and message (`RuntimeError`, `ValueError`, `AnalysisException`, `SparkException`,
  a Python traceback).
- Earlier runs of the same item succeeded.

## Diagnostic Steps
1. Open the run in the Monitoring hub and read the traceback and the driver log: the failing cell, line and exception.
2. Compare with the last successful run: was the notebook, its parameters or its input data changed in between?
3. Check the deployment history of the item (a recent deployment is a contributing change, not proof).
4. Check the input tables the cell reads for new nulls, new values or an unexpected volume.
5. Confirm it is not capacity (throttling, rejected Spark sessions) or a credential problem; those have their own runbooks.

## Possible Causes
- A code change that is wrong for some input.
- A data change: new values, nulls, a type change, an empty input.
- A bug that only shows at a certain volume or date.
- A dependency (library, environment) changed under the item.

## Recommended Remediation
1. Reproduce the failing cell with the failing input in DEV.
2. Fix the code or the data cause in Git (DEV), promote through the deployment pipeline (DEV, TEST, PROD). Do not hot-edit PROD.
3. Rerun the item (or the pipeline that runs it) after the deployment completes.
4. If downstream data products are late, tell their owners.

## Validation
- The rerun completes and the item's output is complete (row counts within the normal range).
- Downstream refreshes and the data product SLA are back on track.

## Rollback
- Revert the commit and redeploy the previous version of the item.

## Escalation
- The item's owner, with the traceback and the run id.
- Platform on-call when several items fail with the same exception at once.
