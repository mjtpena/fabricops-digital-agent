"""Pure functions mapping adapter records onto :class:`OperationalEvent`."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

from orqentis.domain.common import Environment, ItemType
from orqentis.domain.delta import SchemaChange
from orqentis.domain.estate import Capacity, DeltaTableDeclaration
from orqentis.domain.events import EventSeverity, EventType, OperationalEvent
from orqentis.domain.runs import ActivityRun, CapacitySnapshot, PipelineRun, Refresh, RunStatus
from orqentis.observability.redaction import redact_text
from orqentis.telemetry.delta import SchemaSnapshot, clip

CAPACITY_SATURATION_PERCENT = 100.0


def _event_time(start: datetime | None, end: datetime | None) -> datetime:
    when = end or start
    if when is None:
        raise ValueError("Run has neither start nor end time")
    return when


def pipeline_run_event(run: PipelineRun, *, display_name: str, environment: Environment) -> OperationalEvent:
    failed = run.status.is_failure
    message = redact_text(run.failure.message) if run.failure else None
    return OperationalEvent(
        event_id=f"evt-pipeline-run-{run.id}",
        event_type=EventType.PIPELINE_RUN_FAILED if failed else EventType.PIPELINE_RUN_SUCCEEDED,
        tenant_id=run.tenant_id,
        workspace_id=run.workspace_id,
        item_id=run.item_id,
        item_type=ItemType.DATA_PIPELINE,
        display_name=display_name,
        environment=environment,
        timestamp=_event_time(run.start_time, run.end_time),
        severity=EventSeverity.ERROR if failed else EventSeverity.INFO,
        summary=(f"Pipeline {display_name} run {run.id} {run.status.value}" + (f": {message}" if message else "")),
        correlation_id=run.id,
        dedupe_key=f"pipeline-run:{run.id}:{run.status.value}",
        source=run.source,
        payload={
            "run_id": run.id,
            "status": run.status.value,
            "invoke_type": run.invoke_type,
            "start_time": run.start_time.isoformat() if run.start_time else None,
            "end_time": run.end_time.isoformat() if run.end_time else None,
            "duration_seconds": run.duration_seconds,
            "error_code": run.failure.error_code if run.failure else None,
            "error_message": message,
            "root_activity_id": run.correlation_id,
        },
    )


def activity_failure_event(activity: ActivityRun, *, pipeline_name: str, environment: Environment) -> OperationalEvent:
    message = redact_text(activity.error.message) if activity.error else "Activity failed"
    return OperationalEvent(
        event_id=f"evt-activity-{activity.id}",
        event_type=EventType.ACTIVITY_RUN_FAILED,
        tenant_id=activity.tenant_id,
        workspace_id=activity.workspace_id,
        item_id=activity.pipeline_item_id,
        item_type=ItemType.DATA_PIPELINE,
        display_name=pipeline_name,
        environment=environment,
        timestamp=_event_time(activity.start_time, activity.end_time),
        severity=EventSeverity.ERROR,
        summary=f"Activity {activity.activity_name} ({activity.activity_type}) failed: {message}",
        correlation_id=activity.pipeline_run_id,
        dedupe_key=f"activity-run:{activity.id}:{activity.status.value}",
        source=activity.source,
        payload={
            "run_id": activity.pipeline_run_id,
            "activity_run_id": activity.id,
            "activity_name": activity.activity_name,
            "activity_type": activity.activity_type,
            "target_item_id": activity.target_item_id,
            "error_code": activity.error.error_code if activity.error else None,
            "error_message": message,
        },
    )


def refresh_event(refresh: Refresh, *, display_name: str, environment: Environment) -> OperationalEvent:
    failed = refresh.status.is_failure
    message = redact_text(refresh.error.message) if refresh.error else None
    return OperationalEvent(
        event_id=f"evt-refresh-{refresh.id}",
        event_type=EventType.REFRESH_FAILED if failed else EventType.REFRESH_SUCCEEDED,
        tenant_id=refresh.tenant_id,
        workspace_id=refresh.workspace_id,
        item_id=refresh.item_id,
        item_type=ItemType.SEMANTIC_MODEL,
        display_name=display_name,
        environment=environment,
        timestamp=_event_time(refresh.start_time, refresh.end_time),
        severity=EventSeverity.ERROR if failed else EventSeverity.INFO,
        summary=f"Semantic model {display_name} refresh {refresh.status.value}" + (f": {message}" if message else ""),
        correlation_id=refresh.id,
        dedupe_key=f"refresh:{refresh.id}:{refresh.status.value}",
        source=refresh.source,
        payload={
            "refresh_id": refresh.id,
            "refresh_type": refresh.refresh_type,
            "status": refresh.status.value,
            "duration_seconds": refresh.duration_seconds,
            "error_message": message,
        },
    )


# Item types whose job instances are collected next to pipelines (ADR-0020): (succeeded, failed) event types, the noun
# used in summaries and the prefix of ids and dedupe keys.
JOB_EVENTS: dict[ItemType, tuple[EventType, EventType, str, str]] = {
    ItemType.NOTEBOOK: (EventType.NOTEBOOK_RUN_SUCCEEDED, EventType.NOTEBOOK_RUN_FAILED, "Notebook", "notebook-run"),
    ItemType.SPARK_JOB_DEFINITION: (
        EventType.SPARK_JOB_SUCCEEDED,
        EventType.SPARK_JOB_FAILED,
        "Spark job definition",
        "spark-job",
    ),
    ItemType.DATAFLOW: (
        EventType.DATAFLOW_REFRESH_SUCCEEDED,
        EventType.DATAFLOW_REFRESH_FAILED,
        "Dataflow",
        "dataflow-refresh",
    ),
}

# The invoke types that name another item as the starter of a job: a pipeline's activity, a notebook run from a notebook
# (the Monitoring hub calls that a reference run). Only these suppress a job incident. A value nobody has seen yet is
# treated as a direct run: a failure is never hidden behind a word we do not know, and when a pipeline incident does
# cover it the engine's coverage check (incidents/detection.py) drops the duplicate.
_LAUNCHER_INVOKE_TYPES = frozenset({"pipeline", "referencerun", "notebook"})


def launched_by_item(invoke_type: str | None) -> bool:
    return invoke_type is not None and invoke_type.strip().lower() in _LAUNCHER_INVOKE_TYPES


def item_job_event(
    run: PipelineRun, *, item_type: ItemType, display_name: str, environment: Environment
) -> OperationalEvent:
    """A finished job instance of a notebook, Spark job definition or Dataflow as an event."""
    succeeded_type, failed_type, noun, prefix = JOB_EVENTS[item_type]
    failed = run.status.is_failure
    message = redact_text(run.failure.message) if run.failure else None
    return OperationalEvent(
        event_id=f"evt-{prefix}-{run.id}",
        event_type=failed_type if failed else succeeded_type,
        tenant_id=run.tenant_id,
        workspace_id=run.workspace_id,
        item_id=run.item_id,
        item_type=item_type,
        display_name=display_name,
        environment=environment,
        timestamp=_event_time(run.start_time, run.end_time),
        severity=EventSeverity.ERROR if failed else EventSeverity.INFO,
        summary=f"{noun} {display_name} run {run.id} {run.status.value}" + (f": {message}" if message else ""),
        correlation_id=run.id,
        dedupe_key=f"{prefix}:{run.id}:{run.status.value}",
        source=run.source,
        payload={
            "run_id": run.id,
            "status": run.status.value,
            "invoke_type": run.invoke_type,
            "launched_by_item": launched_by_item(run.invoke_type),
            "job_type": run.metadata.get("job_type"),
            "start_time": run.start_time.isoformat() if run.start_time else None,
            "end_time": run.end_time.isoformat() if run.end_time else None,
            "duration_seconds": run.duration_seconds,
            "error_code": run.failure.error_code if run.failure else None,
            "error_message": message,
            "root_activity_id": run.correlation_id,
        },
    )


_MAX_LISTED_COLUMNS = 100


def schema_change_event(
    change: SchemaChange,
    *,
    declaration: DeltaTableDeclaration,
    tenant_id: str,
    workspace_id: str,
    lakehouse_name: str,
    environment: Environment,
    timestamp: datetime,
    source: str,
    user_metadata: str | None = None,
    truncated: bool = False,
) -> OperationalEvent:
    """A schema change of a declared Delta table. Every name in it came from the table's log: untrusted, bounded."""
    removed = [c.name for c in change.removed][:_MAX_LISTED_COLUMNS]
    added = [c.name for c in change.added][:_MAX_LISTED_COLUMNS]
    parts: list[str] = []
    if change.renames:
        parts.append("renamed " + ", ".join(f"{r.before} -> {r.after}" for r in change.renames[:10]))
    if removed:
        parts.append("removed " + ", ".join(removed[:10]))
    if added:
        parts.append("added " + ", ".join(added[:10]))
    if change.type_changes:
        parts.append(
            "type changed " + ", ".join(f"{t.column} ({t.before} -> {t.after})" for t in change.type_changes[:10])
        )
    table = change.table
    return OperationalEvent(
        event_id=f"evt-schema-{declaration.id}-v{change.to_version}",
        event_type=EventType.SCHEMA_CHANGED,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        item_id=declaration.item_id,
        item_type=ItemType.LAKEHOUSE,
        display_name=lakehouse_name,
        environment=environment,
        timestamp=timestamp,
        severity=EventSeverity.WARNING if change.is_breaking else EventSeverity.INFO,
        summary=clip(
            f"Schema change on {lakehouse_name}.{table} (Delta version {change.from_version} -> {change.to_version}): "
            + "; ".join(parts),
            600,
        ),
        dedupe_key=f"schema:{declaration.item_id}:{declaration.schema_name or ''}:{table}:{change.to_version}",
        source=source,
        payload={
            "table": table,
            "schema": declaration.schema_name,
            "previous_version": change.from_version,
            "current_version": change.to_version,
            "removed_columns": removed,
            "added_columns": added,
            "type_changes": [t.model_dump() for t in change.type_changes[:_MAX_LISTED_COLUMNS]],
            "renamed_columns": [r.model_dump() for r in change.renames[:_MAX_LISTED_COLUMNS]],
            "removed_count": len(change.removed),
            "added_count": len(change.added),
            "operation": change.operation,
            "user_metadata": user_metadata,
            "log_truncated": truncated,
        },
    )


def schema_snapshot_event(
    *,
    declaration: DeltaTableDeclaration,
    version: int,
    schema: SchemaSnapshot | None,
    tenant_id: str,
    workspace_id: str,
    lakehouse_name: str,
    environment: Environment,
    timestamp: datetime,
    source: str,
    checkpoint: bool = False,
) -> OperationalEvent:
    """The schema of a declared Delta table at ``version``: what the next run compares against.

    ``schema`` is ``None`` for a *checkpoint* of a table whose schema was never seen: it only records how far the log
    was read, so a run that stopped early resumes after it. Snapshots are INFO events no rule consumes.
    """
    digest = hashlib.sha256(
        json.dumps(schema.to_payload() if schema else None, sort_keys=True).encode("utf-8")
    ).hexdigest()[:12]
    table = declaration.table
    return OperationalEvent(
        event_id=f"evt-schema-snapshot-{declaration.id}-v{version}-{digest}",
        event_type=EventType.SCHEMA_SNAPSHOT,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        item_id=declaration.item_id,
        item_type=ItemType.LAKEHOUSE,
        display_name=lakehouse_name,
        environment=environment,
        timestamp=timestamp,
        summary=clip(f"Schema of {lakehouse_name}.{table} at Delta version {version}", 300),
        correlation_id=declaration.id,
        dedupe_key=f"schema-snapshot:{declaration.item_id}:{declaration.schema_name or ''}:{table}:{version}:{digest}",
        source=source,
        payload={
            "table": table,
            "schema": declaration.schema_name,
            "version": version,
            "checkpoint": checkpoint,
            **(schema.to_payload() if schema else {"columns": None, "physical": {}, "columns_truncated": False}),
        },
    )


def capacity_events(snapshot: CapacitySnapshot, capacity: Capacity) -> list[OperationalEvent]:
    stamp = snapshot.timestamp.isoformat()
    payload = {
        "cu_utilization_percent": snapshot.cu_utilization_percent,
        "interactive_delay_percent": snapshot.interactive_delay_percent,
        "background_rejection_percent": snapshot.background_rejection_percent,
        "top_workloads": [w.model_dump(mode="json") for w in snapshot.top_workloads],
    }
    events = [
        OperationalEvent(
            event_id=f"evt-capacity-{capacity.id}-{int(snapshot.timestamp.timestamp())}",
            event_type=EventType.CAPACITY_METRIC,
            tenant_id=capacity.tenant_id,
            item_id=capacity.id,
            display_name=capacity.display_name,
            environment=capacity.environment,
            timestamp=snapshot.timestamp,
            summary=f"Capacity {capacity.display_name} at {snapshot.cu_utilization_percent:.0f}% CU",
            dedupe_key=f"capacity-metric:{capacity.id}:{stamp}",
            source=snapshot.source,
            payload=payload,
        )
    ]
    if snapshot.is_throttling or snapshot.cu_utilization_percent >= CAPACITY_SATURATION_PERCENT:
        events.append(
            OperationalEvent(
                event_id=f"evt-throttling-{capacity.id}-{int(snapshot.timestamp.timestamp())}",
                event_type=EventType.CAPACITY_THROTTLING,
                tenant_id=capacity.tenant_id,
                item_id=capacity.id,
                display_name=capacity.display_name,
                environment=capacity.environment,
                timestamp=snapshot.timestamp,
                severity=EventSeverity.WARNING,
                summary=(
                    f"Capacity {capacity.display_name} throttling: {snapshot.cu_utilization_percent:.0f}% CU, "
                    f"interactive delay {snapshot.interactive_delay_percent:.0f}%, "
                    f"background rejection {snapshot.background_rejection_percent:.0f}%"
                ),
                dedupe_key=f"capacity-throttling:{capacity.id}:{stamp}",
                source=snapshot.source,
                payload=payload,
            )
        )
    return events


def is_reportable(status: RunStatus) -> bool:
    return status.is_terminal and status not in {RunStatus.SKIPPED, RunStatus.DEDUPED}
