"""The lineage-scanner port used by ``orqentis contracts scaffold --scanner``.

Lineage between semantic models, reports and their data sources is only available from the Power BI admin
Scanner API, which needs a Fabric administrator. The scaffold takes it through this port so that it can be
verified against recorded responses, and so that nothing in the application runtime depends on it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ScannedReport:
    id: str
    workspace_id: str
    dataset_id: str | None  # the semantic model the report is bound to


@dataclass(frozen=True)
class ScannedDataset:
    id: str  # the semantic model's id, which is also its Fabric item id
    workspace_id: str
    upstream_dataset_ids: tuple[str, ...] = ()
    datasource_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ScannedDatasource:
    id: str
    kind: str  # e.g. Sql, Web, AzureDataLakeStorage
    detail: str  # server/database or URL host and path, without credentials or query strings


@dataclass(frozen=True)
class LineageScan:
    reports: tuple[ScannedReport, ...] = ()
    datasets: tuple[ScannedDataset, ...] = ()
    datasources: tuple[ScannedDatasource, ...] = ()


class LineageScanner(Protocol):
    """Read-only lineage for a set of workspaces. Implementations must not mutate anything."""

    async def scan(self, workspace_ids: Sequence[str]) -> LineageScan: ...

    async def aclose(self) -> None: ...
