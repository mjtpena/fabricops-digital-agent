"""Shared domain primitives: base model, identifiers, environments and item types."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict


def utcnow() -> datetime:
    """Timezone-aware 'now' in UTC. Prefer an injected ``Clock`` in application code."""
    return datetime.now(UTC)


def new_id(prefix: str) -> str:
    """Short, prefixed, collision-resistant identifier (e.g. ``APR-3f9a1c2d4e5b``)."""
    return f"{prefix}-{uuid4().hex[:12]}"


class DomainModel(BaseModel):
    """Base class for every domain model.

    ``extra="forbid"`` makes fixture or adapter drift fail loudly instead of silently
    dropping fields.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, validate_assignment=True)


class Environment(StrEnum):
    DEV = "DEV"
    TEST = "TEST"
    PROD = "PROD"
    UNKNOWN = "UNKNOWN"


class ItemType(StrEnum):
    """Fabric item types plus the logical node types FabricOps reasons about."""

    DATA_PIPELINE = "DataPipeline"
    NOTEBOOK = "Notebook"
    LAKEHOUSE = "Lakehouse"
    WAREHOUSE = "Warehouse"
    SEMANTIC_MODEL = "SemanticModel"
    REPORT = "Report"
    SPARK_JOB_DEFINITION = "SparkJobDefinition"
    DATAFLOW = "Dataflow"
    # Logical (non-Fabric) nodes: declared by platform teams, not discovered from Fabric.
    DATA_PRODUCT = "DataProduct"
    EXTERNAL_SOURCE = "ExternalSource"
    OTHER = "Other"


Metadata = dict[str, Any]
