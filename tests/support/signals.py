"""Small builders for the WS7 collector, rule and workflow tests: items, catalogs and runs (no Fabric behind)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from orqentis.domain.common import Environment, ItemType
from orqentis.domain.estate import (
    Dataflow,
    DeltaTableDeclaration,
    FabricItem,
    GenericItem,
    Lakehouse,
    Notebook,
    Pipeline,
    SemanticModel,
    SparkJobDefinition,
    Workspace,
)
from orqentis.domain.events import EventSeverity, EventType, OperationalEvent
from orqentis.domain.runs import FailureDetail, PipelineRun, Refresh, RunStatus
from orqentis.topology.catalog import EstateCatalog

TENANT = "tenant-test"
WS = "ws-1"
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


def at(hour: int, minute: int = 0, second: int = 0, *, day: int = 9) -> datetime:
    return datetime(2026, 10, day, hour, minute, second, tzinfo=UTC)


def catalog(
    *items: FabricItem, tables: list[DeltaTableDeclaration] | None = None, env: Environment = Environment.PROD
) -> EstateCatalog:
    c = EstateCatalog()
    c.workspaces[WS] = Workspace(id=WS, tenant_id=TENANT, display_name="Sales [PROD]", environment=env)
    for item in items:
        c.items[item.id] = item
    c.delta_tables = {t.id: t for t in tables or []}
    return c


def notebook(item_id: str = "nb-1", name: str = "transform", env: Environment = Environment.PROD) -> Notebook:
    return Notebook(id=item_id, tenant_id=TENANT, workspace_id=WS, display_name=name, environment=env)


def spark_job(
    item_id: str = "sj-1", name: str = "spark_etl", env: Environment = Environment.PROD
) -> SparkJobDefinition:
    return SparkJobDefinition(id=item_id, tenant_id=TENANT, workspace_id=WS, display_name=name, environment=env)


def dataflow(item_id: str = "df-1", name: str = "load_dim", env: Environment = Environment.PROD) -> Dataflow:
    return Dataflow(id=item_id, tenant_id=TENANT, workspace_id=WS, display_name=name, environment=env)


def pipeline(item_id: str = "pl-1", name: str = "ingest") -> Pipeline:
    return Pipeline(id=item_id, tenant_id=TENANT, workspace_id=WS, display_name=name, environment=Environment.PROD)


def lakehouse(item_id: str = "lh-1", name: str = "bronze") -> Lakehouse:
    return Lakehouse(id=item_id, tenant_id=TENANT, workspace_id=WS, display_name=name, environment=Environment.PROD)


def model(item_id: str = "sm-1", name: str = "SalesModel") -> SemanticModel:
    return SemanticModel(id=item_id, tenant_id=TENANT, workspace_id=WS, display_name=name, environment=Environment.PROD)


def monitoring_database(name: str = "Monitoring KQL database") -> GenericItem:
    return GenericItem(
        id="kql-1",
        tenant_id=TENANT,
        workspace_id=WS,
        display_name=name,
        environment=Environment.PROD,
        fabric_type="KQLDatabase",
    )


def job_run(
    run_id: str,
    item_id: str,
    status: RunStatus,
    start: datetime,
    end: datetime,
    *,
    invoke: str | None = "Manual",
    item_type: ItemType = ItemType.NOTEBOOK,
    message: str | None = None,
    code: str | None = None,
) -> PipelineRun:
    return PipelineRun(
        id=run_id,
        tenant_id=TENANT,
        workspace_id=WS,
        item_id=item_id,
        item_type=item_type,
        environment=Environment.PROD,
        status=status,
        invoke_type=invoke,
        start_time=start,
        end_time=end,
        failure=FailureDetail(message=message, error_code=code) if message else None,
    )


def refresh_run(
    refresh_id: str, item_id: str, status: RunStatus, start: datetime, end: datetime, *, message: str | None = None
) -> Refresh:
    return Refresh(
        id=refresh_id,
        tenant_id=TENANT,
        workspace_id=WS,
        item_id=item_id,
        status=status,
        start_time=start,
        end_time=end,
        error=FailureDetail(message=message, error_code="ModelRefresh_ShortMessage_ProcessingError")
        if message
        else None,
    )


def event(
    event_type: EventType,
    *,
    item_id: str = "nb-1",
    item_type: ItemType | None = ItemType.NOTEBOOK,
    when: datetime = NOW,
    event_id: str | None = None,
    correlation_id: str | None = "run-1",
    environment: Environment = Environment.PROD,
    payload: dict[str, Any] | None = None,
    workspace_id: str | None = WS,
    name: str = "transform",
) -> OperationalEvent:
    return OperationalEvent(
        event_id=event_id or f"evt-{event_type.value.lower()}-{item_id}-{int(when.timestamp())}",
        event_type=event_type,
        tenant_id=TENANT,
        workspace_id=workspace_id,
        item_id=item_id,
        item_type=item_type,
        display_name=name,
        environment=environment,
        timestamp=when,
        severity=EventSeverity.ERROR,
        summary=f"{event_type.value} {name}",
        correlation_id=correlation_id,
        dedupe_key=f"{event_type.value}:{item_id}:{correlation_id}:{when.isoformat()}",
        source="fabric.jobs",
        payload=payload or {},
    )
