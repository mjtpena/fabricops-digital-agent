"""Execution telemetry: pipeline runs, activity runs, refreshes and capacity snapshots."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import AwareDatetime, Field

from orqentis.domain.common import DomainModel, Environment, ItemType, Metadata


class RunStatus(StrEnum):
    """Normalised run status. Adapters map vendor statuses (e.g. Fabric ``Completed``) onto this."""

    NOT_STARTED = "NotStarted"
    QUEUED = "Queued"
    IN_PROGRESS = "InProgress"
    SUCCEEDED = "Succeeded"
    FAILED = "Failed"
    CANCELLED = "Cancelled"
    SKIPPED = "Skipped"
    TIMED_OUT = "TimedOut"
    DEDUPED = "Deduped"

    @property
    def is_terminal(self) -> bool:
        return self not in {RunStatus.NOT_STARTED, RunStatus.QUEUED, RunStatus.IN_PROGRESS}

    @property
    def is_failure(self) -> bool:
        return self in {RunStatus.FAILED, RunStatus.TIMED_OUT}


class FailureDetail(DomainModel):
    message: str
    error_code: str | None = None
    failure_type: str | None = None
    request_id: str | None = None


def _duration(start: datetime | None, end: datetime | None) -> float | None:
    if start is None or end is None:
        return None
    return (end - start).total_seconds()


class PipelineRun(DomainModel):
    """A pipeline job instance."""

    id: str
    tenant_id: str
    workspace_id: str
    item_id: str
    item_type: ItemType = ItemType.DATA_PIPELINE
    display_name: str | None = None
    environment: Environment = Environment.UNKNOWN
    status: RunStatus
    invoke_type: str | None = None
    start_time: AwareDatetime | None = None
    end_time: AwareDatetime | None = None
    failure: FailureDetail | None = None
    correlation_id: str | None = None
    source: str = "fabric.jobs"
    metadata: Metadata = Field(default_factory=dict)

    @property
    def duration_seconds(self) -> float | None:
        return _duration(self.start_time, self.end_time)

    @property
    def observed_at(self) -> datetime | None:
        """When the run last changed state as far as telemetry is concerned: its end, else its start."""
        return self.end_time or self.start_time


class ActivityRun(DomainModel):
    id: str
    pipeline_run_id: str
    tenant_id: str
    workspace_id: str
    pipeline_item_id: str
    activity_name: str
    activity_type: str
    status: RunStatus
    start_time: AwareDatetime | None = None
    end_time: AwareDatetime | None = None
    target_item_id: str | None = None
    error: FailureDetail | None = None
    output: Metadata = Field(default_factory=dict)
    correlation_id: str | None = None
    source: str = "fabric.pipelines"
    metadata: Metadata = Field(default_factory=dict)

    @property
    def duration_seconds(self) -> float | None:
        return _duration(self.start_time, self.end_time)


class Refresh(DomainModel):
    """A semantic model refresh."""

    id: str
    tenant_id: str
    workspace_id: str
    item_id: str
    item_type: ItemType = ItemType.SEMANTIC_MODEL
    refresh_type: str = "Scheduled"
    status: RunStatus
    start_time: AwareDatetime | None = None
    end_time: AwareDatetime | None = None
    error: FailureDetail | None = None
    correlation_id: str | None = None
    source: str = "powerbi.refreshes"
    metadata: Metadata = Field(default_factory=dict)

    @property
    def duration_seconds(self) -> float | None:
        return _duration(self.start_time, self.end_time)


class WorkloadUsage(DomainModel):
    item_id: str
    workspace_id: str
    display_name: str
    item_type: ItemType
    cu_percent: float = Field(ge=0)


class CapacitySnapshot(DomainModel):
    """Point-in-time capacity utilisation, as exposed by the Capacity Metrics app or Eventhouse."""

    capacity_id: str
    timestamp: AwareDatetime
    cu_utilization_percent: float = Field(ge=0)
    interactive_delay_percent: float = Field(default=0, ge=0)
    background_rejection_percent: float = Field(default=0, ge=0)
    top_workloads: list[WorkloadUsage] = Field(default_factory=list)
    source: str = "fabric.capacity-metrics"

    @property
    def is_throttling(self) -> bool:
        return self.interactive_delay_percent > 0 or self.background_rejection_percent > 0
