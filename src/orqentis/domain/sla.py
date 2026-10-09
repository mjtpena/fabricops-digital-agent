"""Data product operational contracts (SLAs) and their evaluated state."""

from __future__ import annotations

from datetime import time
from enum import StrEnum

from pydantic import AwareDatetime, Field

from orqentis.domain.common import DomainModel, Metadata


class SLA(DomainModel):
    """e.g. ``sales_gold must be available before 06:30 Australia/Sydney``."""

    id: str
    tenant_id: str
    data_product_id: str
    display_name: str
    description: str
    deadline_local_time: time
    timezone: str
    # ISO weekdays (1=Monday) on which the contract applies.
    applies_on_weekdays: list[int] = Field(default_factory=lambda: [1, 2, 3, 4, 5, 6, 7])
    # Item whose successful run refreshes the data product (usually the producing pipeline).
    freshness_item_id: str
    owner: str
    source: str = "declared"
    metadata: Metadata = Field(default_factory=dict)


class SLAState(StrEnum):
    MET = "MET"
    ON_TRACK = "ON_TRACK"
    AT_RISK = "AT_RISK"
    BREACHED = "BREACHED"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    UNKNOWN = "UNKNOWN"


class SLAAssessment(DomainModel):
    sla_id: str
    data_product_id: str
    data_product_name: str
    description: str
    state: SLAState
    deadline: AwareDatetime | None = None
    evaluated_at: AwareDatetime
    last_successful_update: AwareDatetime | None = None
    minutes_to_deadline: float | None = None
    blocking_item_ids: list[str] = Field(default_factory=list)
    owner: str
    reason: str
