"""Operational metrics (OpenTelemetry), behind one small class.

Everything that is counted or timed goes through :class:`Metrics`, so the instruments, their names and their
attributes live in one place and the rest of the code base imports nothing from OpenTelemetry. This module uses the
OpenTelemetry *API* only: it records into whatever ``MeterProvider`` is installed. With none installed (the default,
``make demo``, the unit tests) every call is a no-op; with ``APPLICATIONINSIGHTS_CONNECTION_STRING`` set the Azure
Monitor distro installs a provider and the metrics land in the ``AppMetrics`` table of the Log Analytics workspace
(see ``docs/operations.md``); ``ORQENTIS_OTEL_CONSOLE_EXPORTER=true`` prints them instead (``tracing.py`` wires both).
Tests build ``Metrics(provider.get_meter(...))`` on a private provider with an in-memory reader.

Attribute values are bounded on purpose. They never carry anything a Fabric item, a model or a caller chose (item
names, error messages, tool names the model made up): a value is an enum member, one of our own collector, job or tool
names, or ``unknown``. That keeps cardinality small and keeps untrusted metadata out of telemetry.

Instruments (all names start with ``orqentis.``):

====================================  =========  ===========================================================
name                                  kind       attributes
====================================  =========  ===========================================================
``collector.duration`` (s)            histogram  collector
``collector.runs``                    counter    collector, outcome = ok | error
``collector.errors``                  counter    collector (one per failed collector or scope)
``events.ingested``                   counter    collector (events that were new to the event store)
``collection.runs``                   counter    outcome = success | degraded | failed | cancelled
``collection.heartbeat`` (s, epoch)   gauge      time of the last successful collection (see below)
``incidents.opened``                  counter    category, severity, environment
``incidents.detection_latency`` (s)   histogram  category, severity, environment (failure time to incident)
``investigation.runs``                counter    outcome, trigger
``investigation.duration`` (s)        histogram  outcome, trigger
``tool.calls``                        counter    tool, status
``tool.errors``                       counter    tool (status ERROR only)
``llm.tokens``                        counter    model, direction = input | output
``llm.cost`` (USD)                    counter    model (estimate from configured prices; absent when unset)
``reasoner.fallbacks``                counter    reasoner (the LLM reasoner failed, the rule-based one answered)
``approvals.pending``                 gauge      (open approval requests that have not expired)
``scheduler.job.runs``                counter    job, outcome = ok | failed | timeout | cancelled
``scheduler.job.failures``            counter    job, reason = failed | timeout
``scheduler.job.overruns``            counter    job (a slot was skipped because the previous run was still going)
``scheduler.job.duration`` (s)        histogram  job, outcome
``scheduler.lease.changes``           counter    event = acquired | lost
``scheduler.leader``                  gauge      1 while this replica holds the leader lease, else 0
``notifications``                     counter    channel, outcome = sent | failed | dropped | abandoned (see below)
``audit.verifications``               counter    result = valid | invalid | error
``audit.anchor.age`` (s)              gauge      age of the newest immutable audit anchor (PostgreSQL backend)
``contracts.reload.failures``         counter    error_type (a refused or missing contracts file on a reload)
====================================  =========  ===========================================================

**Heartbeat.** ``collection.heartbeat`` is the time of the last collection that did not fail outright: the run
finished, detection ran, and at least one collector (or scope of a scoped collector) succeeded. It is *not* advanced
by a run in which every collector failed (for example an expired credential), by a cancelled run, or by one that raised.
A run where only some collectors failed is ``degraded``: the heartbeat advances (FabricOps is not blind) and
``collector.runs{outcome=error}`` is what alerts. Alerts use the ``collection.runs`` counter for "no successful
collection in N minutes" because a counter that is absent is easy to detect; the gauge is for dashboards and people.

**Notifications.** The dispatcher (``notifications/dispatcher.py``) records one outcome per message when it ends;
retries are attempts, not outcomes. ``sent``: delivered. ``failed``: refused for good, or the attempts ran out.
``dropped``: the bounded outbox was full and the oldest message made room. ``abandoned``: the process stopped first.
The channel is the adapter's own name (``teams_workflows``, ``webhook``), never a URL.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Final

from opentelemetry import metrics as otel_metrics
from opentelemetry.metrics import CallbackOptions, Meter, NoOpMeterProvider, Observation

from orqentis import __version__

METER_NAME: Final = "orqentis"
UNKNOWN: Final = "unknown"
_MAX_LABEL = 64


def label(value: object | None) -> str:
    """A bounded attribute value: ``str(value)`` truncated, ``unknown`` when absent."""
    if value is None:
        return UNKNOWN
    text = str(getattr(value, "value", value)).strip()
    return text[:_MAX_LABEL] if text else UNKNOWN


class Metrics:
    """Typed recording helpers over the OpenTelemetry instruments of FabricOps."""

    def __init__(self, meter: Meter | None = None) -> None:
        meter = meter or otel_metrics.get_meter(METER_NAME, __version__)
        self._last_collection_success: float | None = None
        self._approvals_pending: int | None = None
        self._leader = False
        self._anchor_age: float | None = None

        # collection
        self._collector_duration = meter.create_histogram(
            "orqentis.collector.duration", unit="s", description="Time one collector took in one collection run"
        )
        self._collector_runs = meter.create_counter(
            "orqentis.collector.runs", description="Collector runs by outcome (error: any collector or scope failed)"
        )
        self._collector_errors = meter.create_counter(
            "orqentis.collector.errors", description="Failed collector runs, or failed scopes of a scoped collector"
        )
        self._events_ingested = meter.create_counter(
            "orqentis.events.ingested", description="Events that were new to the event store, by collector"
        )
        self._collection_runs = meter.create_counter(
            "orqentis.collection.runs", description="Collection runs (collect + detect) by outcome"
        )
        meter.create_observable_gauge(
            "orqentis.collection.heartbeat",
            callbacks=[self._observe_heartbeat],
            unit="s",
            description="Unix time of the last successful collection; absent until the first one",
        )
        # detection and incidents
        self._incidents_opened = meter.create_counter(
            "orqentis.incidents.opened", description="Incidents opened by a detection rule"
        )
        self._detection_latency = meter.create_histogram(
            "orqentis.incidents.detection_latency",
            unit="s",
            description="Seconds from the failure event to the incident being opened",
        )
        # investigations, tools, model
        self._investigation_runs = meter.create_counter(
            "orqentis.investigation.runs", description="Investigation jobs by outcome and trigger"
        )
        self._investigation_duration = meter.create_histogram(
            "orqentis.investigation.duration", unit="s", description="Run time of an investigation job"
        )
        self._tool_calls = meter.create_counter("orqentis.tool.calls", description="Tool invocations by status")
        self._tool_errors = meter.create_counter("orqentis.tool.errors", description="Tool invocations that failed")
        self._llm_tokens = meter.create_counter("orqentis.llm.tokens", description="Model tokens used")
        self._llm_cost = meter.create_counter(
            "orqentis.llm.cost", unit="USD", description="Estimated model cost from the configured token prices"
        )
        self._reasoner_fallbacks = meter.create_counter(
            "orqentis.reasoner.fallbacks",
            description="Times the rule-based reasoner replaced a failed primary reasoner",
        )
        meter.create_observable_gauge(
            "orqentis.approvals.pending",
            callbacks=[self._observe_approvals],
            description="Approval requests waiting for a human decision",
        )
        # scheduler
        self._job_runs = meter.create_counter("orqentis.scheduler.job.runs", description="Scheduled job runs")
        self._job_failures = meter.create_counter(
            "orqentis.scheduler.job.failures", description="Scheduled job runs that failed or timed out"
        )
        self._job_overruns = meter.create_counter(
            "orqentis.scheduler.job.overruns", description="Slots skipped because the previous run was still going"
        )
        self._job_duration = meter.create_histogram(
            "orqentis.scheduler.job.duration", unit="s", description="Run time of a scheduled job"
        )
        self._lease_changes = meter.create_counter(
            "orqentis.scheduler.lease.changes", description="Leader lease acquired or lost by this replica"
        )
        meter.create_observable_gauge(
            "orqentis.scheduler.leader",
            callbacks=[self._observe_leader],
            description="1 while this replica holds the scheduler leader lease",
        )
        # notifications and audit
        self._notifications = meter.create_counter(
            "orqentis.notifications", description="Notifications by how they ended (sent, failed, dropped, abandoned)"
        )
        self._audit_verifications = meter.create_counter(
            "orqentis.audit.verifications", description="Audit chain verifications by result"
        )
        meter.create_observable_gauge(
            "orqentis.audit.anchor.age",
            callbacks=[self._observe_anchor_age],
            unit="s",
            description="Age of the newest immutable audit anchor; absent when none was seen (alert above 2x interval)",
        )
        self._contracts_reload_failures = meter.create_counter(
            "orqentis.contracts.reload.failures",
            description="Reloads of the estate contracts file that were refused (the previous contracts stay in force)",
        )

    # ------------------------------------------------------------------------------------------ observers
    def _observe_heartbeat(self, options: CallbackOptions) -> Iterable[Observation]:
        if self._last_collection_success is not None:
            yield Observation(self._last_collection_success)

    def _observe_approvals(self, options: CallbackOptions) -> Iterable[Observation]:
        if self._approvals_pending is not None:
            yield Observation(self._approvals_pending)

    def _observe_leader(self, options: CallbackOptions) -> Iterable[Observation]:
        yield Observation(1 if self._leader else 0)

    def _observe_anchor_age(self, options: CallbackOptions) -> Iterable[Observation]:
        if self._anchor_age is not None:
            yield Observation(self._anchor_age)

    # ------------------------------------------------------------------------------------------ collection
    def collector_run(self, collector: str, *, seconds: float, errors: int, new_events: int) -> None:
        attrs = {"collector": label(collector)}
        self._collector_duration.record(seconds, attrs)
        self._collector_runs.add(1, {**attrs, "outcome": "error" if errors else "ok"})
        if errors:
            self._collector_errors.add(errors, attrs)
        if new_events:
            self._events_ingested.add(new_events, attrs)

    def collection_run(self, outcome: str, *, at: datetime | None = None) -> None:
        """Record a finished collection run. ``at`` (when the run succeeded or degraded) advances the heartbeat."""
        self._collection_runs.add(1, {"outcome": label(outcome)})
        if at is not None and outcome in {"success", "degraded"}:
            self._last_collection_success = max(self._last_collection_success or 0.0, at.timestamp())

    @property
    def last_collection_success(self) -> float | None:
        return self._last_collection_success

    # ------------------------------------------------------------------------------------------ incidents
    def incident_opened(
        self, *, category: object, severity: object, environment: object, detection_latency_seconds: float
    ) -> None:
        attrs = {"category": label(category), "severity": label(severity), "environment": label(environment)}
        self._incidents_opened.add(1, attrs)
        self._detection_latency.record(max(detection_latency_seconds, 0.0), attrs)

    def investigation_finished(self, *, outcome: str, trigger: object, seconds: float) -> None:
        attrs = {"outcome": label(outcome), "trigger": label(trigger)}
        self._investigation_runs.add(1, attrs)
        self._investigation_duration.record(max(seconds, 0.0), attrs)

    # ------------------------------------------------------------------------------------------ tools, model
    def tool_call(self, tool: str, status: object) -> None:
        attrs = {"tool": label(tool)}
        self._tool_calls.add(1, {**attrs, "status": label(status)})
        if label(status) == "ERROR":
            self._tool_errors.add(1, attrs)

    def llm_usage(
        self,
        *,
        model: str | None,
        input_tokens: int | None,
        output_tokens: int | None,
        cost_usd: float | None = None,
    ) -> None:
        name = label(model)
        if input_tokens:
            self._llm_tokens.add(input_tokens, {"model": name, "direction": "input"})
        if output_tokens:
            self._llm_tokens.add(output_tokens, {"model": name, "direction": "output"})
        if cost_usd:
            self._llm_cost.add(cost_usd, {"model": name})

    def reasoner_fallback(self, reasoner: str) -> None:
        self._reasoner_fallbacks.add(1, {"reasoner": label(reasoner)})

    def set_approvals_pending(self, count: int) -> None:
        self._approvals_pending = max(count, 0)

    # ------------------------------------------------------------------------------------------ scheduler
    def job_finished(self, job: str, *, outcome: str, seconds: float) -> None:
        attrs = {"job": label(job)}
        self._job_runs.add(1, {**attrs, "outcome": label(outcome)})
        self._job_duration.record(max(seconds, 0.0), {**attrs, "outcome": label(outcome)})
        if outcome in {"failed", "timeout"}:
            self._job_failures.add(1, {**attrs, "reason": outcome})

    def job_overrun(self, job: str) -> None:
        self._job_overruns.add(1, {"job": label(job)})

    def lease_changed(self, event: str) -> None:
        self._lease_changes.add(1, {"event": label(event)})

    def set_leader(self, leader: bool) -> None:
        self._leader = leader

    # ------------------------------------------------------------------------------------------ hooks
    def notification(self, channel: str, outcome: str) -> None:
        """A notification ended: ``sent``, ``failed``, ``dropped`` (outbox full) or ``abandoned`` (shutdown)."""
        self._notifications.add(1, {"channel": label(channel), "outcome": label(outcome)})

    def audit_verification(self, result: str) -> None:
        self._audit_verifications.add(1, {"result": label(result)})

    def audit_anchor_age(self, seconds: float | None) -> None:
        """The age of the newest anchor as the last verification saw it; ``None`` (no anchor) clears the reading."""
        self._anchor_age = max(seconds, 0.0) if seconds is not None else None

    def contracts_reload_failed(self, error_type: str) -> None:
        self._contracts_reload_failures.add(1, {"error_type": label(error_type)})


DISABLED: Final = Metrics(NoOpMeterProvider().get_meter(METER_NAME))

_shared: Metrics | None = None


def get_metrics() -> Metrics:
    """The process-wide :class:`Metrics` on the global meter provider (created on first use).

    Observable gauges register a callback per ``Metrics`` instance, so the application creates exactly one; a container
    that is rebuilt after a failed start reuses it instead of registering a second set.
    """
    global _shared
    if _shared is None:
        _shared = Metrics()
    return _shared
