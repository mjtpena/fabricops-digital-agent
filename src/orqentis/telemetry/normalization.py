"""Pure functions mapping adapter records onto :class:`OperationalEvent`."""

from __future__ import annotations

from datetime import datetime

from orqentis.domain.common import Environment, ItemType
from orqentis.domain.estate import Capacity
from orqentis.domain.events import EventSeverity, EventType, OperationalEvent
from orqentis.domain.runs import ActivityRun, CapacitySnapshot, PipelineRun, Refresh, RunStatus
from orqentis.observability.redaction import redact_text

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
