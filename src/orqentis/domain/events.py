"""Normalised operational events: the single currency of the event store."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import AwareDatetime, Field

from orqentis.domain.common import DomainModel, Environment, ItemType, Metadata


class EventType(StrEnum):
    PIPELINE_RUN_SUCCEEDED = "PIPELINE_RUN_SUCCEEDED"
    PIPELINE_RUN_FAILED = "PIPELINE_RUN_FAILED"
    ACTIVITY_RUN_FAILED = "ACTIVITY_RUN_FAILED"
    REFRESH_SUCCEEDED = "REFRESH_SUCCEEDED"
    REFRESH_FAILED = "REFRESH_FAILED"
    CAPACITY_METRIC = "CAPACITY_METRIC"
    CAPACITY_THROTTLING = "CAPACITY_THROTTLING"
    SCHEMA_CHANGED = "SCHEMA_CHANGED"
    DEPLOYMENT_COMPLETED = "DEPLOYMENT_COMPLETED"
    CREDENTIAL_EXPIRED = "CREDENTIAL_EXPIRED"
    INCIDENT_CREATED = "INCIDENT_CREATED"
    INCIDENT_STATUS_CHANGED = "INCIDENT_STATUS_CHANGED"
    INVESTIGATION_COMPLETED = "INVESTIGATION_COMPLETED"


class EventSeverity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class OperationalEvent(DomainModel):
    event_id: str
    event_type: EventType
    tenant_id: str
    workspace_id: str | None = None
    item_id: str | None = None
    item_type: ItemType | None = None
    display_name: str | None = None
    environment: Environment = Environment.UNKNOWN
    timestamp: AwareDatetime
    severity: EventSeverity = EventSeverity.INFO
    summary: str
    correlation_id: str | None = None
    dedupe_key: str
    source: str
    payload: dict[str, Any] = Field(default_factory=dict)
    metadata: Metadata = Field(default_factory=dict)
