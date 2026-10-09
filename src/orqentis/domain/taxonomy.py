"""Incident taxonomy and severity.

Built-in categories are a ``StrEnum``; deployments can register additional categories at
start-up (``TAXONOMY.register``) without changing code that consumes ``CategoryName``.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import AfterValidator


class IncidentCategory(StrEnum):
    PIPELINE_FAILURE = "PIPELINE_FAILURE"
    PIPELINE_TIMEOUT = "PIPELINE_TIMEOUT"
    PIPELINE_DEGRADATION = "PIPELINE_DEGRADATION"
    ACTIVITY_FAILURE = "ACTIVITY_FAILURE"
    AUTH_FAILURE = "AUTH_FAILURE"
    CONNECTIVITY_FAILURE = "CONNECTIVITY_FAILURE"
    SCHEMA_DRIFT = "SCHEMA_DRIFT"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    LAKEHOUSE_WRITE_FAILURE = "LAKEHOUSE_WRITE_FAILURE"
    SEMANTIC_MODEL_REFRESH_FAILURE = "SEMANTIC_MODEL_REFRESH_FAILURE"
    DATA_STALENESS = "DATA_STALENESS"
    SLA_BREACH = "SLA_BREACH"
    CAPACITY_PRESSURE = "CAPACITY_PRESSURE"
    THROTTLING = "THROTTLING"
    DEPLOYMENT_DRIFT = "DEPLOYMENT_DRIFT"
    CONFIGURATION_DRIFT = "CONFIGURATION_DRIFT"
    UNKNOWN = "UNKNOWN"


class Taxonomy:
    """Registry of known incident categories (built-in + registered extensions)."""

    def __init__(self) -> None:
        self._custom: dict[str, str] = {}

    def register(self, name: str, description: str = "") -> str:
        normalized = name.strip().upper()
        if not normalized.replace("_", "").isalnum():
            raise ValueError(f"Invalid category name: {name!r}")
        self._custom[normalized] = description
        return normalized

    def unregister(self, name: str) -> None:
        self._custom.pop(name.strip().upper(), None)

    def is_known(self, name: str) -> bool:
        return name in IncidentCategory.__members__ or name in self._custom

    def all(self) -> list[str]:
        return [c.value for c in IncidentCategory] + sorted(self._custom)


TAXONOMY = Taxonomy()


def validate_category(value: str) -> str:
    normalized = value.strip().upper()
    if not TAXONOMY.is_known(normalized):
        raise ValueError(f"Unknown incident category {value!r}; register it with TAXONOMY.register()")
    return normalized


CategoryName = Annotated[str, AfterValidator(validate_category)]


class Severity(StrEnum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"

    @property
    def rank(self) -> int:
        return {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}[self.value]
