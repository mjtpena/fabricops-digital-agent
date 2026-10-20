"""Operational metrics: each one is emitted with the right attributes in the right situation.

Time is a fake clock and the readings come from an in-memory reader on a private meter provider, so nothing waits and
nothing is exported.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

import pytest

from orqentis.config.clock import FixedClock
from orqentis.domain.events import EventType, OperationalEvent
from orqentis.fabric.errors import FabricUnavailableError
from orqentis.observability.metrics import DISABLED, UNKNOWN, get_metrics, label
from orqentis.runtime.collection import CollectionService
from orqentis.runtime.lease import InProcessLeaderLease, LeaseTable
from orqentis.runtime.scheduler import JobContext, JobSpec, Scheduler
from orqentis.storage.event_store import LocalEventStore
from orqentis.storage.sqlite import SqliteDatabase
from orqentis.telemetry.collectors import Collector, TelemetryIngestor
from tests.support.metrics import MetricsHarness

T0 = datetime(2026, 10, 20, 8, 0, tzinfo=UTC)
MINUTE = timedelta(minutes=1)
HOUR = timedelta(hours=1)


# ----------------------------------------------------------------------------------------------- the Metrics class
class Colour(StrEnum):
    RED = "RED"


def test_labels_are_bounded_and_never_empty() -> None:
    assert label(None) == UNKNOWN and label("") == UNKNOWN and label("   ") == UNKNOWN
    assert label(Colour.RED) == "RED"  # an enum member is labelled by its value, not its repr
    assert len(label("x" * 500)) == 64


def test_the_disabled_metrics_and_the_shared_instance_accept_every_call_without_a_provider() -> None:
    for metrics in (DISABLED, get_metrics()):
        metrics.collector_run("pipeline-runs", seconds=1.0, errors=1, new_events=2)
        metrics.collection_run("success", at=T0)
        metrics.incident_opened(category="X", severity="HIGH", environment="PROD", detection_latency_seconds=5)
        metrics.investigation_finished(outcome="succeeded", trigger="API", seconds=3)
        metrics.tool_call("t", "ERROR")
        metrics.llm_usage(model="m", input_tokens=1, output_tokens=2, cost_usd=0.1)
        metrics.reasoner_fallback("agent_framework")
        metrics.set_approvals_pending(3)
        metrics.job_finished("collect-detect", outcome="failed", seconds=1)
        metrics.job_overrun("collect-detect")
        metrics.lease_changed("acquired")
        metrics.set_leader(True)
        metrics.notification("teams", "sent")
        metrics.audit_verification("valid")
    assert get_metrics() is get_metrics()


def test_every_recording_helper_uses_the_documented_instrument_and_attributes() -> None:
    h = MetricsHarness()
    m = h.metrics
    m.collector_run("pipeline-runs", seconds=2.0, errors=0, new_events=4)
    m.collector_run("pipeline-runs", seconds=1.0, errors=3, new_events=0)
    m.incident_opened(category="SCHEMA_DRIFT", severity=Colour.RED, environment="PROD", detection_latency_seconds=90)
    m.investigation_finished(outcome="succeeded", trigger="API", seconds=12.5)
    m.tool_call("get_pipeline_run", "SUCCESS")
    m.tool_call("get_pipeline_run", "ERROR")
    m.llm_usage(model="gpt-test", input_tokens=1000, output_tokens=500, cost_usd=0.006)
    m.reasoner_fallback("agent_framework")
    m.job_finished("collect-detect", outcome="timeout", seconds=30)
    m.job_overrun("collect-detect")
    m.lease_changed("lost")
    m.notification("teams", "failed")
    m.audit_verification("invalid")

    assert h.total("orqentis.collector.runs", collector="pipeline-runs", outcome="ok") == 1
    assert h.total("orqentis.collector.runs", collector="pipeline-runs", outcome="error") == 1
    assert h.total("orqentis.collector.errors", collector="pipeline-runs") == 3
    assert h.total("orqentis.events.ingested", collector="pipeline-runs") == 4
    assert h.histogram("orqentis.collector.duration", collector="pipeline-runs") == (2, 3.0)
    assert h.total("orqentis.incidents.opened", category="SCHEMA_DRIFT", severity="RED", environment="PROD") == 1
    assert h.histogram("orqentis.incidents.detection_latency", category="SCHEMA_DRIFT") == (1, 90.0)
    assert h.total("orqentis.investigation.runs", outcome="succeeded", trigger="API") == 1
    assert h.histogram("orqentis.investigation.duration", outcome="succeeded") == (1, 12.5)
    assert h.total("orqentis.tool.calls", tool="get_pipeline_run", status="SUCCESS") == 1
    assert h.total("orqentis.tool.calls", tool="get_pipeline_run", status="ERROR") == 1
    assert h.total("orqentis.tool.errors", tool="get_pipeline_run") == 1  # only ERROR counts as an error
    assert h.total("orqentis.llm.tokens", model="gpt-test", direction="input") == 1000
    assert h.total("orqentis.llm.tokens", model="gpt-test", direction="output") == 500
    assert h.total("orqentis.llm.cost", model="gpt-test") == pytest.approx(0.006)
    assert h.total("orqentis.reasoner.fallbacks", reasoner="agent_framework") == 1
    assert h.total("orqentis.scheduler.job.runs", job="collect-detect", outcome="timeout") == 1
    assert h.total("orqentis.scheduler.job.failures", job="collect-detect", reason="timeout") == 1
    assert h.histogram("orqentis.scheduler.job.duration", job="collect-detect") == (1, 30.0)
    assert h.total("orqentis.scheduler.job.overruns", job="collect-detect") == 1
    assert h.total("orqentis.scheduler.lease.changes", event="lost") == 1
    assert h.total("orqentis.notifications", channel="teams", outcome="failed") == 1
    assert h.total("orqentis.audit.verifications", result="invalid") == 1


def test_a_cost_is_only_emitted_when_there_is_one() -> None:
    h = MetricsHarness()
    h.metrics.llm_usage(model="m", input_tokens=10, output_tokens=None, cost_usd=None)
    assert h.total("orqentis.llm.tokens", direction="input") == 10
    assert "orqentis.llm.cost" not in h.names()
    assert "orqentis.llm.tokens" in h.names()


def test_gauges_report_state_and_stay_absent_until_there_is_some() -> None:
    h = MetricsHarness()
    assert h.gauge("orqentis.collection.heartbeat") is None  # nothing has succeeded yet
    assert h.gauge("orqentis.approvals.pending") is None
    assert h.gauge("orqentis.scheduler.leader") == 0
    h.metrics.set_approvals_pending(4)
    h.metrics.set_approvals_pending(-1)  # a failed count must not turn into a negative reading
    assert h.gauge("orqentis.approvals.pending") == 0
    h.metrics.set_leader(True)
    assert h.gauge("orqentis.scheduler.leader") == 1


# ---------------------------------------------------------------------------------------- collection and heartbeat
def event(key: str, at: datetime) -> OperationalEvent:
    return OperationalEvent(
        event_id=f"evt-{key}",
        event_type=EventType.PIPELINE_RUN_SUCCEEDED,
        tenant_id="tenant-test",
        timestamp=at,
        summary=key,
        dedupe_key=key,
        source="test",
    )


class FakeCollector:
    """Takes ``seconds`` of fake time to answer, returns ``events`` and can be told to fail."""

    def __init__(self, name: str, clock: FixedClock, *, seconds: float = 0.0) -> None:
        self.name = name
        self.events: list[OperationalEvent] = []
        self.fail: BaseException | None = None
        self._clock = clock
        self._seconds = seconds

    async def collect(self, *, since: datetime, until: datetime) -> list[OperationalEvent]:
        self._clock.advance(timedelta(seconds=self._seconds))
        if self.fail is not None:
            raise self.fail
        return [e for e in self.events if since <= e.timestamp <= until]


class FakeScopes:
    """A scoped collector: ``failing`` scopes raise a Fabric error."""

    def __init__(self, name: str, scopes: list[str]) -> None:
        self.name = name
        self._scopes = scopes
        self.failing: set[str] = set()

    def scopes(self) -> list[str]:
        return list(self._scopes)

    async def collect_scope(self, scope: str, *, since: datetime, until: datetime) -> list[OperationalEvent]:
        if scope in self.failing:
            raise unavailable()
        return []


def unavailable() -> FabricUnavailableError:
    return FabricUnavailableError("Fabric is down", status_code=503, error_code="Unavailable", retryable=True)


class FakeDetection:
    def __init__(self) -> None:
        self.fail: BaseException | None = None
        self.runs = 0

    async def run(self) -> list[object]:
        self.runs += 1
        if self.fail is not None:
            raise self.fail
        return []


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[LocalEventStore]:
    db = SqliteDatabase(tmp_path / "events.sqlite3")
    yield LocalEventStore(db)
    db.close()


def collection(
    collectors: list[Collector], store: LocalEventStore, clock: FixedClock, h: MetricsHarness
) -> tuple[CollectionService, FakeDetection]:
    detection = FakeDetection()
    ingestor = TelemetryIngestor(collectors, store, clock, metrics=h.metrics)
    return CollectionService(ingestor, detection, metrics=h.metrics, clock=clock), detection  # type: ignore[arg-type]


async def test_each_collector_reports_its_duration_runs_errors_and_new_events(store: LocalEventStore) -> None:
    clock = FixedClock(T0)
    h = MetricsHarness()
    pipelines = FakeCollector("pipeline-runs", clock, seconds=2.5)
    pipelines.events = [event("a", T0 - MINUTE), event("b", T0 - MINUTE)]
    refreshes = FakeCollector("semantic-model-refreshes", clock, seconds=1.0)
    refreshes.fail = unavailable()
    scoped = FakeScopes("capacity-metrics", ["c1", "c2", "c3"])
    scoped.failing = {"c2", "c3"}
    service, _ = collection([pipelines, refreshes, scoped], store, clock, h)

    await service.run()

    assert h.histogram("orqentis.collector.duration", collector="pipeline-runs") == (1, 2.5)
    assert h.histogram("orqentis.collector.duration", collector="semantic-model-refreshes") == (1, 1.0)
    assert h.total("orqentis.collector.runs", collector="pipeline-runs", outcome="ok") == 1
    assert h.total("orqentis.collector.runs", collector="semantic-model-refreshes", outcome="error") == 1
    assert h.total("orqentis.collector.errors", collector="semantic-model-refreshes") == 1
    assert h.total("orqentis.collector.errors", collector="capacity-metrics") == 2  # one per failing scope ...
    assert h.total("orqentis.collector.runs", collector="capacity-metrics", outcome="error") == 1  # ... one failing run
    assert h.total("orqentis.events.ingested", collector="pipeline-runs") == 2

    await service.run()  # the same events again: already in the store, so none is new
    assert h.total("orqentis.events.ingested", collector="pipeline-runs") == 2
    assert h.total("orqentis.collector.runs", collector="pipeline-runs", outcome="ok") == 2


async def test_the_heartbeat_is_the_time_of_the_last_successful_collection(store: LocalEventStore) -> None:
    clock = FixedClock(T0)
    h = MetricsHarness()
    service, detection = collection([FakeCollector("pipeline-runs", clock)], store, clock, h)
    assert h.gauge("orqentis.collection.heartbeat") is None

    await service.run()
    assert h.gauge("orqentis.collection.heartbeat") == T0.timestamp()
    assert h.total("orqentis.collection.runs", outcome="success") == 1

    clock.advance(5 * MINUTE)
    await service.run()
    assert h.gauge("orqentis.collection.heartbeat") == (T0 + 5 * MINUTE).timestamp()
    assert detection.runs == 2


async def test_the_heartbeat_does_not_advance_when_every_collector_fails(store: LocalEventStore) -> None:
    clock = FixedClock(T0)
    h = MetricsHarness()
    pipelines = FakeCollector("pipeline-runs", clock)
    scoped = FakeScopes("semantic-model-refreshes", ["m1", "m2"])
    service, _ = collection([pipelines, scoped], store, clock, h)
    await service.run()
    healthy = h.gauge("orqentis.collection.heartbeat")
    assert healthy == T0.timestamp()

    # An expired credential or a Fabric outage: every collector and every scope fails, yet the run "completes".
    clock.advance(5 * MINUTE)
    pipelines.fail = unavailable()
    scoped.failing = {"m1", "m2"}
    await service.run()

    assert h.gauge("orqentis.collection.heartbeat") == healthy  # still the old time: this run did not count
    assert h.total("orqentis.collection.runs", outcome="failed") == 1
    assert h.total("orqentis.collection.runs", outcome="success") == 1


async def test_a_partly_failing_collection_is_degraded_and_still_moves_the_heartbeat(store: LocalEventStore) -> None:
    clock = FixedClock(T0)
    h = MetricsHarness()
    refreshes = FakeCollector("semantic-model-refreshes", clock)
    refreshes.fail = unavailable()  # for example the Viewer-only 403 on refresh history
    pipelines = FakeScopes("pipeline-runs", ["p1", "p2"])
    pipelines.failing = {"p2"}
    service, _ = collection([refreshes, pipelines], store, clock, h)

    await service.run()

    assert h.total("orqentis.collection.runs", outcome="degraded") == 1
    assert h.gauge("orqentis.collection.heartbeat") == T0.timestamp()
    # What alerts is the collector that keeps failing, not the heartbeat.
    assert h.total("orqentis.collector.runs", collector="semantic-model-refreshes", outcome="error") == 1


async def test_an_exception_a_failing_detection_or_a_cancelled_run_never_moves_the_heartbeat(
    store: LocalEventStore,
) -> None:
    clock = FixedClock(T0)
    h = MetricsHarness()
    collector = FakeCollector("pipeline-runs", clock)
    service, detection = collection([collector], store, clock, h)

    collector.fail = RuntimeError("a bug in a collector")  # not a FabricError: the run itself blows up
    with pytest.raises(RuntimeError):
        await service.run()
    collector.fail = asyncio.CancelledError()  # a deploy or a lost lease cuts the run short
    with pytest.raises(asyncio.CancelledError):
        await service.run()
    collector.fail = None
    detection.fail = RuntimeError("detection store unavailable")  # ingestion worked, detection did not
    with pytest.raises(RuntimeError):
        await service.run()

    assert h.gauge("orqentis.collection.heartbeat") is None
    assert h.total("orqentis.collection.runs", outcome="failed") == 2
    assert h.total("orqentis.collection.runs", outcome="cancelled") == 1
    assert h.total("orqentis.collection.runs", outcome="success") == 0
    assert h.total("orqentis.collector.runs", collector="pipeline-runs", outcome="error") == 1  # the bug counts too


async def test_a_scoped_collector_that_blows_up_is_counted_and_the_error_propagates(store: LocalEventStore) -> None:
    clock = FixedClock(T0)
    h = MetricsHarness()

    class Broken(FakeScopes):
        async def collect_scope(self, scope: str, *, since: datetime, until: datetime) -> list[OperationalEvent]:
            if scope == "p2":
                raise RuntimeError("a bug, not a Fabric error")
            return await super().collect_scope(scope, since=since, until=until)

    service, _ = collection([Broken("pipeline-runs", ["p1", "p2"])], store, clock, h)
    with pytest.raises(RuntimeError):
        await service.run()

    assert h.total("orqentis.collector.runs", collector="pipeline-runs", outcome="error") == 1
    assert h.total("orqentis.collector.errors", collector="pipeline-runs") == 1
    assert h.total("orqentis.collection.runs", outcome="failed") == 1
    assert h.gauge("orqentis.collection.heartbeat") is None


async def test_an_estate_with_nothing_to_collect_is_not_blind(store: LocalEventStore) -> None:
    clock = FixedClock(T0)
    h = MetricsHarness()
    service, _ = collection([FakeScopes("pipeline-runs", [])], store, clock, h)
    await service.run()
    assert h.total("orqentis.collection.runs", outcome="success") == 1


# ---------------------------------------------------------------------------------------------------- scheduler
class Job:
    def __init__(self) -> None:
        self.runs = 0
        self.fail = False
        self.gate: asyncio.Event | None = None
        self.seconds = 0.0
        self.clock: FixedClock | None = None

    async def __call__(self, ctx: JobContext) -> None:
        self.runs += 1
        if self.clock is not None:
            self.clock.advance(timedelta(seconds=self.seconds))
        if self.gate is not None:
            await self.gate.wait()
        if self.fail:
            raise RuntimeError("boom")


def make_scheduler(
    jobs: list[JobSpec], clock: FixedClock, h: MetricsHarness, *, holder: str = "a", table: LeaseTable | None = None
) -> Scheduler:
    lease = InProcessLeaderLease(clock, holder_id=holder, table=table)
    return Scheduler(jobs, lease, clock, lease_ttl=2 * MINUTE, metrics=h.metrics)


async def test_the_scheduler_reports_runs_failures_durations_and_overruns() -> None:
    clock = FixedClock(T0)
    h = MetricsHarness()
    job = Job()
    job.clock, job.seconds = clock, 7.0
    sched = make_scheduler([JobSpec("collect-detect", 5 * MINUTE, job)], clock, h)

    await sched.tick()
    await sched.wait_idle()
    assert h.total("orqentis.scheduler.job.runs", job="collect-detect", outcome="ok") == 1
    assert h.histogram("orqentis.scheduler.job.duration", job="collect-detect") == (1, 7.0)
    assert h.total("orqentis.scheduler.job.failures") == 0

    clock.advance(5 * MINUTE)
    job.fail = True
    await sched.tick()
    await sched.wait_idle()
    assert h.total("orqentis.scheduler.job.runs", job="collect-detect", outcome="failed") == 1
    assert h.total("orqentis.scheduler.job.failures", job="collect-detect", reason="failed") == 1

    # A run still going when its next slot arrives: the slot is skipped and counted as an overrun.
    job.fail = False
    job.gate = asyncio.Event()
    clock.advance(5 * MINUTE)
    await sched.tick()
    await asyncio.sleep(0)
    clock.advance(5 * MINUTE)
    await sched.tick()
    clock.advance(5 * MINUTE)
    await sched.tick()
    assert h.total("orqentis.scheduler.job.overruns", job="collect-detect") == 2
    job.gate.set()
    await sched.wait_idle()


async def test_a_job_that_times_out_is_a_failure_with_reason_timeout() -> None:
    clock = FixedClock(T0)
    h = MetricsHarness()
    hang = Job()
    hang.gate = asyncio.Event()
    sched = make_scheduler([JobSpec("hang", MINUTE, hang, timeout=timedelta(milliseconds=50))], clock, h)
    await sched.tick()
    await sched.wait_idle()
    assert h.total("orqentis.scheduler.job.runs", job="hang", outcome="timeout") == 1
    assert h.total("orqentis.scheduler.job.failures", job="hang", reason="timeout") == 1


async def test_lease_changes_and_the_leader_gauge_follow_leadership() -> None:
    clock = FixedClock(T0)
    h = MetricsHarness()
    table = LeaseTable()
    stuck = Job()
    stuck.gate = asyncio.Event()
    old = make_scheduler([JobSpec("work", MINUTE, stuck)], clock, h, holder="replica-a", table=table)
    other = MetricsHarness()
    new = make_scheduler([JobSpec("work", MINUTE, Job())], clock, other, holder="replica-b", table=table)

    assert h.gauge("orqentis.scheduler.leader") == 0
    await old.tick()
    await asyncio.sleep(0)  # let the job begin: only a job that was running can be cancelled
    assert h.total("orqentis.scheduler.lease.changes", event="acquired") == 1
    assert h.gauge("orqentis.scheduler.leader") == 1

    clock.advance(3 * MINUTE)  # replica-a stalls past its lease and replica-b takes over
    await new.tick()
    assert other.total("orqentis.scheduler.lease.changes", event="acquired") == 1
    assert other.gauge("orqentis.scheduler.leader") == 1

    await old.tick()  # replica-a wakes up, cannot renew and steps down
    await old.wait_idle()
    assert h.total("orqentis.scheduler.lease.changes", event="lost") == 1
    assert h.gauge("orqentis.scheduler.leader") == 0
    assert h.total("orqentis.scheduler.job.runs", job="work", outcome="cancelled") == 1
    assert h.total("orqentis.scheduler.job.failures") == 0  # being deposed is not a job failure

    await new.stop()
    assert other.gauge("orqentis.scheduler.leader") == 0
