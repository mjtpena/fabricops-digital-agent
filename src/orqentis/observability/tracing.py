"""OpenTelemetry tracing, designed to export to Azure Application Insights."""

from __future__ import annotations

import importlib
from typing import Any

from agent_framework.observability import enable_instrumentation
from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import ConsoleMetricExporter, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

from orqentis import __version__
from orqentis.observability.logging import get_logger

_log = get_logger(__name__)
_configured = False


def configure_tracing(
    *, service_name: str = "orqentis-fabricops", console: bool = False, appinsights_connection_string: str | None
) -> None:
    """Install a tracer provider (and, for the console exporter, a meter provider) once per process.

    With ``APPLICATIONINSIGHTS_CONNECTION_STRING`` set and the ``azure-monitor`` extra installed,
    traces and metrics flow to Application Insights via the Azure Monitor distro (the application's own
    metrics are in ``orqentis.observability.metrics``). Agent Framework GenAI spans (model, token usage)
    are enabled; prompt/message content is not recorded because it contains Fabric metadata.

    Nothing is exported by default: without a connection string and without ``console`` there is no
    exporter, so spans are dropped and metric calls are no-ops. ``console`` prints spans and metrics.
    """
    global _configured
    if _configured:
        return
    resource = Resource.create({"service.name": service_name, "service.version": __version__})
    exported = False
    if appinsights_connection_string:
        try:
            azure_monitor: Any = importlib.import_module("azure.monitor.opentelemetry")
        except ModuleNotFoundError:
            _log.warning("azure-monitor-opentelemetry not installed; Application Insights export disabled")
        else:
            azure_monitor.configure_azure_monitor(connection_string=appinsights_connection_string, resource=resource)
            exported = True
    if not exported:
        provider = TracerProvider(resource=resource)
        if console:
            provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
            metrics.set_meter_provider(
                MeterProvider(
                    resource=resource, metric_readers=[PeriodicExportingMetricReader(ConsoleMetricExporter())]
                )
            )
        trace.set_tracer_provider(provider)
    enable_instrumentation(enable_sensitive_data=False)
    _configured = True


def get_tracer() -> trace.Tracer:
    return trace.get_tracer("orqentis")


def span_trace_id(span: trace.Span) -> str | None:
    ctx = span.get_span_context()
    return format(ctx.trace_id, "032x") if ctx.is_valid else None
