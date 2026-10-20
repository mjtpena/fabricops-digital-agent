"""Read back what ``Metrics`` recorded: a private meter provider with an in-memory reader, never a global one."""

from __future__ import annotations

from typing import Any

from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import HistogramDataPoint, InMemoryMetricReader, NumberDataPoint

from orqentis.observability.metrics import Metrics


class MetricsHarness:
    """``Metrics`` on a private provider. Counters are cumulative, so ``total`` is "everything recorded so far"."""

    def __init__(self) -> None:
        self.reader = InMemoryMetricReader()
        self.provider = MeterProvider(metric_readers=[self.reader])
        self.metrics = Metrics(self.provider.get_meter("orqentis-test"))

    def _points(self, name: str) -> list[Any]:
        data = self.reader.get_metrics_data()
        if data is None:
            return []
        return [
            point
            for resource in data.resource_metrics
            for scope in resource.scope_metrics
            for metric in scope.metrics
            if metric.name == name
            for point in metric.data.data_points
        ]

    @staticmethod
    def _matches(point: Any, attrs: dict[str, str]) -> bool:
        return all(point.attributes.get(key) == value for key, value in attrs.items())

    def names(self) -> set[str]:
        data = self.reader.get_metrics_data()
        if data is None:
            return set()
        return {m.name for r in data.resource_metrics for s in r.scope_metrics for m in s.metrics}

    def attribute_keys(self, name: str) -> set[str]:
        return {key for point in self._points(name) for key in point.attributes}

    def total(self, name: str, **attrs: str) -> float:
        """Sum of a counter over the data points whose attributes include ``attrs``."""
        return sum(p.value for p in self._points(name) if isinstance(p, NumberDataPoint) and self._matches(p, attrs))

    def gauge(self, name: str) -> float | None:
        values = [p.value for p in self._points(name) if isinstance(p, NumberDataPoint)]
        return values[-1] if values else None

    def histogram(self, name: str, **attrs: str) -> tuple[int, float]:
        """``(count, sum)`` of a histogram over the matching data points."""
        points = [p for p in self._points(name) if isinstance(p, HistogramDataPoint) and self._matches(p, attrs)]
        return sum(p.count for p in points), sum(p.sum for p in points)
