"""Regression tests for the WS1 review findings. Each one was reproduced against the first version of the PR.

1. A queued investigation expired before it ever ran, and cancelled/queued jobs burned the retry budget.
2. A collection cut short (deploy, lease loss, job timeout, a later collector failing) lost detection for the
   events that were already stored, forever.
3. A pipeline that appeared through a catalog refresh was blind for everything before the collector's watermark.
4. Two revisions writing one audit file forked the hash chain.
5. Operational details leaked through the readiness probe, and the lease generation was redacted from logs.
10. An incident on an item that was removed from the estate failed its automatic investigation three times.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from orqentis.api.app import create_app
from orqentis.bootstrap import Container, build_container
from orqentis.config.clock import FixedClock
from orqentis.config.settings import Settings
from orqentis.domain.common import Environment
from orqentis.domain.estate import Pipeline
from orqentis.domain.incidents import Incident, IncidentStatus, TimelineKind
from orqentis.domain.runs import FailureDetail, PipelineRun, RunStatus
from orqentis.fabric.mock import MockFabricClient
from orqentis.incidents.workflows.base import InvestigationTrace
from orqentis.observability.audit import LocalAuditLog
from orqentis.runtime.investigations import InvestigationJobRunner, InvestigationJobStatus, InvestigationTrigger
from orqentis.runtime.lease import InProcessLeaderLease, LeaseTable
from orqentis.runtime.scheduler import JobContext, JobSpec, Scheduler
from tests.conftest import OPERATOR, detect, make_settings
from tests.support.polling import wait_until

MINUTE = timedelta(minutes=1)


def mock_fabric(container: Container) -> MockFabricClient:
    assert isinstance(container.fabric, MockFabricClient)
    return container.fabric


async def clone_incident(container: Container, source: Incident, incident_id: str) -> Incident:
    clone = source.model_copy(update={"incident_id": incident_id, "correlation_id": f"other-{incident_id}"})
    await container.incidents.repository.save(clone)
    return clone


def fake_investigation(container: Container, seconds: float) -> Any:
    """Replace the investigation with a stand-in that takes ``seconds`` and then reports success."""

    async def investigate(incident_id: str, *, actor: Any) -> tuple[Incident, InvestigationTrace]:
        await asyncio.sleep(seconds)
        incident = await container.incidents.get(incident_id)
        trace = InvestigationTrace(
            trace_id=f"trace-{incident_id}", incident_id=incident_id, workflow="fake", started_at=container.clock.now()
        )
        return incident, trace

    return investigate


# ------------------------------------------------------------------------------------------------ finding 1
async def test_the_investigation_timeout_starts_when_a_job_starts_running_not_when_it_is_queued(
    container: Container, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = await detect(container)
    second = await clone_incident(container, first, "INC-2026-0098")
    monkeypatch.setattr(container.investigations, "investigate", fake_investigation(container, 0.25))
    # One slot, 0.4 s timeout, 0.25 s of work each: the second job waits 0.25 s and then runs 0.25 s. Measured from
    # submission it would "take" 0.5 s and expire, although it never ran longer than 0.25 s.
    runner = InvestigationJobRunner(
        container.investigations,
        container.incidents,
        container.audit,
        container.clock,
        max_concurrency=1,
        timeout=timedelta(seconds=0.4),
    )
    a = await runner.submit(first.incident_id, requested_by=OPERATOR, trigger=InvestigationTrigger.API)
    b = await runner.submit(second.incident_id, requested_by=OPERATOR, trigger=InvestigationTrigger.API)
    await runner.wait_idle()
    assert [a.job.status, b.job.status] == [InvestigationJobStatus.SUCCEEDED] * 2, (a.job.error, b.job.error)
    assert runner.failed_attempts(first.incident_id) == 0 and runner.failed_attempts(second.incident_id) == 0


async def test_shutdown_does_not_burn_the_retry_budget_of_running_or_queued_investigations(
    container: Container, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = await detect(container)
    second = await clone_incident(container, first, "INC-2026-0098")
    monkeypatch.setattr(container.investigations, "investigate", fake_investigation(container, 30))
    runner = InvestigationJobRunner(
        container.investigations, container.incidents, container.audit, container.clock, max_concurrency=1
    )
    running = await runner.submit(first.incident_id, requested_by=OPERATOR, trigger=InvestigationTrigger.API)
    queued = await runner.submit(second.incident_id, requested_by=OPERATOR, trigger=InvestigationTrigger.API)
    await wait_until(lambda: running.job.status is InvestigationJobStatus.RUNNING)

    await runner.shutdown()
    assert running.job.status is InvestigationJobStatus.FAILED and queued.job.status is InvestigationJobStatus.FAILED
    # Being stopped by a deploy says nothing about the incident: it must stay eligible for automatic investigation.
    assert runner.failed_attempts(first.incident_id) == 0
    assert runner.failed_attempts(second.incident_id) == 0


async def test_a_genuine_timeout_still_counts_as_a_failed_attempt(
    container: Container, monkeypatch: pytest.MonkeyPatch
) -> None:
    incident = await detect(container)
    monkeypatch.setattr(container.investigations, "investigate", fake_investigation(container, 30))
    runner = InvestigationJobRunner(
        container.investigations, container.incidents, container.audit, container.clock, timeout=timedelta(seconds=0.2)
    )
    submission = await runner.submit(incident.incident_id, requested_by=OPERATOR, trigger=InvestigationTrigger.API)
    await runner.wait_idle()
    assert submission.job.status is InvestigationJobStatus.FAILED and "Timed out" in (submission.job.error or "")
    assert runner.failed_attempts(incident.incident_id) == 1


# ------------------------------------------------------------------------------------------------ finding 2
async def test_a_collection_cancelled_in_its_last_collector_still_reaches_detection_on_the_next_run(
    container: Container, monkeypatch: pytest.MonkeyPatch
) -> None:
    last = container.ingestor._collectors[-1]
    entered = asyncio.Event()

    async def hang(*, since: datetime, until: datetime) -> list[Any]:
        entered.set()
        await asyncio.Event().wait()
        return []

    original = last.collect
    monkeypatch.setattr(last, "collect", hang)
    task = asyncio.create_task(container.collect_and_detect())
    async with asyncio.timeout(30):
        await entered.wait()
    task.cancel()  # a deploy, a lost lease or the job timeout
    await asyncio.gather(task, return_exceptions=True)
    assert await container.event_store.count() > 0  # the earlier collectors' events were stored

    monkeypatch.setattr(last, "collect", original)
    await container.collect_and_detect()
    open_incidents = [i for i in await container.incidents.repository.list() if i.status.is_open]
    assert [i.incident_id for i in open_incidents] == ["INC-2026-0042"]  # detected, and exactly once


async def test_a_later_collector_failing_does_not_hide_the_events_stored_before_it(
    container: Container, monkeypatch: pytest.MonkeyPatch
) -> None:
    last = container.ingestor._collectors[-1]

    async def explode(*, since: datetime, until: datetime) -> list[Any]:
        raise RuntimeError("replay source corrupted")

    monkeypatch.setattr(last, "collect", explode)
    with pytest.raises(RuntimeError, match="corrupted"):
        await container.collect_and_detect()
    open_incidents = [i for i in await container.incidents.repository.list() if i.status.is_open]
    assert [i.incident_id for i in open_incidents] == ["INC-2026-0042"]


# ------------------------------------------------------------------------------------------------ finding 3
async def test_a_pipeline_added_by_a_catalog_refresh_is_collected_from_before_the_collectors_watermark(
    container: Container,
) -> None:
    clock = container.clock
    assert isinstance(clock, FixedClock)
    await container.collect_and_detect()  # the pipeline-run watermark is now "now"

    fabric = mock_fabric(container)
    now = clock.now()
    fabric.estate.items.append(
        Pipeline(
            id="pl-new-pipeline",
            tenant_id="tenant-contoso",
            workspace_id="ws-sales-prod",
            display_name="ingest_new_prod",
            environment=Environment.PROD,
        )
    )
    fabric.scenario.pipeline_runs.append(
        PipelineRun(
            id="run-new-1",
            tenant_id="tenant-contoso",
            workspace_id="ws-sales-prod",
            item_id="pl-new-pipeline",
            environment=Environment.PROD,
            status=RunStatus.FAILED,
            start_time=now - timedelta(hours=1),
            end_time=now - timedelta(minutes=55),
            failure=FailureDetail(message="Source unreachable"),
            correlation_id="root-new-1",
        )
    )
    # It failed an hour ago, before the watermark; the catalog only learns about the pipeline now.
    diff = await container.catalog_refresher.refresh()
    assert diff.items.added == ["pl-new-pipeline"]

    _, created = await container.collect_and_detect()
    assert [i.primary_item_id for i in created] == ["pl-new-pipeline"]


# ------------------------------------------------------------------------------------------------ finding 4
async def test_two_processes_appending_to_one_audit_file_keep_a_single_valid_chain(tmp_path: Path) -> None:
    """The old and the new revision of a Container App run side by side on the same volume during a deploy."""
    clock = FixedClock(datetime(2026, 10, 20, 8, 0, tzinfo=UTC))
    path = tmp_path / "audit" / "audit.jsonl"
    old, new = LocalAuditLog(path, clock), LocalAuditLog(path, clock)  # both loaded the (empty) chain head at start-up
    for writer, name in [(old, "old"), (new, "new"), (old, "old"), (new, "new")]:
        await writer.record(actor=name, action="x", outcome="OK")
    verification = await old.verify()
    assert verification.valid, verification.reason
    assert verification.records == 4


async def test_shutdown_stops_the_scheduler_and_releases_the_lease(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path, scheduler_enabled=True, scheduler_tick_seconds=0.05))
    async with app.router.lifespan_context(app):
        container = app.state.runtime.container
        assert container is not None
        await wait_until(lambda: container.scheduler.is_leader)
    assert not container.scheduler.is_running and not container.scheduler.is_leader
    other = InProcessLeaderLease(container.clock, holder_id="next-revision", table=container.lease._table)
    assert await other.acquire("orqentis-scheduler", timedelta(seconds=60)) is not None


# ------------------------------------------------------------------------------------------------ finding 5
async def test_the_lease_generation_is_visible_in_the_audit_trail(tmp_path: Path) -> None:
    clock = FixedClock(datetime(2026, 10, 20, 8, 0, tzinfo=UTC))
    audit = LocalAuditLog(tmp_path / "audit.jsonl", clock)
    ran = asyncio.Event()

    async def job(ctx: JobContext) -> None:
        ran.set()

    scheduler = Scheduler(
        [JobSpec("work", MINUTE, job)],
        InProcessLeaderLease(clock, holder_id="replica-a", table=LeaseTable()),
        clock,
        audit=audit,
    )
    await scheduler.tick()
    await scheduler.wait_idle()
    record = next(r for r in await audit.list() if r.action == "scheduler.leadership")
    # "token" in a key name makes the redaction blank the value; the generation must stay readable.
    assert record.details["lease_generation"] == 1 and record.details["holder_id"] == "replica-a"


# ------------------------------------------------------------------------------------------------ finding 7
@pytest.mark.security
async def test_the_readiness_probe_reveals_no_exception_text(tmp_path: Path) -> None:
    async def failing(s: Settings) -> Container:
        raise FileNotFoundError("/var/lib/orqentis/contracts/estate.json")

    app = create_app(
        make_settings(
            tmp_path, startup_grace_seconds=0, build_retry_initial_seconds=0.01, build_retry_max_seconds=0.05
        ),
        container_factory=failing,
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http,
    ):
        await wait_until(lambda: app.state.runtime.status.attempts >= 1)
        body = (await http.get("/health/ready")).json()
        assert body["reason"] == "build_failed" and body["error_type"] == "FileNotFoundError"
        assert "/var/lib" not in str(body) and "estate.json" not in str(body)


# ----------------------------------------------------------------------------------------------- finding 10
@pytest.fixture
async def isolated(tmp_path: Path) -> AsyncIterator[Container]:
    c = await build_container(make_settings(tmp_path), reset=True)
    try:
        yield c
    finally:
        await c.investigation_jobs.shutdown()
        await c.aclose()


async def test_removing_an_item_annotates_its_open_incidents_and_stops_automatic_investigation(
    isolated: Container,
) -> None:
    c = isolated
    incident = await detect(c)
    fabric = mock_fabric(c)
    fabric.estate.items[:] = [i for i in fabric.estate.items if i.id != incident.primary_item_id]

    diff = await c.catalog_refresher.refresh()
    assert incident.primary_item_id in diff.items.removed

    annotated = await c.incidents.get(incident.incident_id)
    notes = [t for t in annotated.timeline if t.kind is TimelineKind.NOTE]
    assert notes and incident.primary_item_id in notes[-1].summary and "removed" in notes[-1].summary.lower()
    assert annotated.status is IncidentStatus.DETECTED  # a human decides what to do; the agent does not close it

    for _ in range(4):  # the auto-investigation job has no business with an incident whose item is gone
        await c.scheduler.tick()
        await c.scheduler.wait_idle()
        await c.investigation_jobs.wait_idle()
        assert isinstance(c.clock, FixedClock)
        c.clock.advance(MINUTE)
    assert c.investigation_jobs.latest_for(incident.incident_id) is None
    assert c.investigation_jobs.failed_attempts(incident.incident_id) == 0
