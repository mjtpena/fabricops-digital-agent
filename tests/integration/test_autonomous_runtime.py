"""Autonomous runtime in mock mode: scheduled detection, automatic and asynchronous investigations."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from orqentis.agents.models import EvidencePackage, ReasonerOutput
from orqentis.agents.rule_based import RuleBasedReasoner
from orqentis.agents.toolbox import AgentToolbox
from orqentis.api.app import create_app
from orqentis.bootstrap import Container, build_container
from orqentis.config.clock import FixedClock
from orqentis.domain.incidents import IncidentStatus
from orqentis.domain.principals import SYSTEM_PRINCIPAL
from orqentis.incidents.investigation import InvestigationNotPossibleError
from orqentis.runtime.investigations import (
    AutoInvestigationTask,
    InvestigationJob,
    InvestigationJobRunner,
    InvestigationJobStatus,
    InvestigationTrigger,
)
from tests.conftest import OPERATOR, detect, make_settings
from tests.support.polling import poll

VIEWER = {"X-Orqentis-User": "victor@contoso.com"}
MINUTE = timedelta(minutes=1)


class GatedReasoner:
    """The rule-based reasoner, but it waits until the test lets it go: a stand-in for a slow LLM."""

    name = "gated"

    def __init__(self) -> None:
        self._inner = RuleBasedReasoner(timezone="Australia/Sydney")
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def diagnose(self, package: EvidencePackage, *, tools: AgentToolbox) -> ReasonerOutput:
        self.started.set()
        await self.release.wait()
        return await self._inner.diagnose(package, tools=tools)


async def settle(container: Container) -> None:
    await container.scheduler.wait_idle()
    await container.investigation_jobs.wait_idle()


async def finished(container: Container, job_id: str, *, within: float = 30.0) -> InvestigationJob:
    async with asyncio.timeout(within):
        while True:
            job = container.investigation_jobs.get(job_id)
            assert job is not None
            if not job.status.is_active:
                return job
            await asyncio.sleep(0.01)


@pytest.fixture
async def gated_container(tmp_path: Path) -> AsyncIterator[tuple[Container, GatedReasoner]]:
    gate = GatedReasoner()
    c = await build_container(make_settings(tmp_path), reset=True, reasoner=gate)
    try:
        yield c, gate
    finally:
        gate.release.set()
        await c.investigation_jobs.shutdown()
        await c.aclose()


# ---------------------------------------------------------------- AC2: scheduled detection -> investigation
@pytest.mark.security
async def test_an_incident_from_scheduled_detection_is_investigated_without_any_api_call(
    container: Container,
) -> None:
    clock = container.clock
    assert isinstance(clock, FixedClock)
    assert not [i for i in await container.incidents.repository.list() if i.status.is_open]  # only closed history

    # Minute 0: the scheduler's first tick collects telemetry and runs detection.
    assert "collect-detect" in await container.scheduler.tick()
    await settle(container)
    detected = await container.incidents.get("INC-2026-0042")
    assert detected.metadata["detection_rule"] == "DET-PIPELINE-FAILED"  # opened by a deterministic rule

    # Minute 1: the auto-investigation job picks the new incident up (if it did not already win the race above).
    clock.advance(MINUTE)
    await container.scheduler.tick()
    await settle(container)

    incident = await container.incidents.get("INC-2026-0042")
    assert incident.status is IncidentStatus.AWAITING_APPROVAL
    assert incident.category == "SCHEMA_DRIFT" and incident.findings and incident.approval_ids
    job = container.investigation_jobs.latest_for("INC-2026-0042")
    assert job is not None and job.status is InvestigationJobStatus.SUCCEEDED
    assert job.trigger is InvestigationTrigger.SCHEDULER and job.requested_by == SYSTEM_PRINCIPAL.id

    audit = await container.audit.list(incident_id="INC-2026-0042")
    starts = [r for r in audit if r.action == "investigation.start"]
    assert [r.actor for r in starts] == [SYSTEM_PRINCIPAL.id]  # exactly one investigation, requested by no human
    assert [r.actor for r in audit if r.action == "incident.create"] == ["system:detection"]
    # Automation stops at the approval boundary: the mutation is only *requested*, for a human to decide.
    pending = [await container.approvals.get(a) for a in incident.approval_ids]
    assert pending and {a.status.value for a in pending} == {"PENDING"}


async def test_auto_investigation_is_not_repeated_for_an_incident_already_handled(container: Container) -> None:
    await detect(container)
    clock = container.clock
    assert isinstance(clock, FixedClock)
    for _ in range(4):
        await container.scheduler.tick()
        await settle(container)
        clock.advance(MINUTE)
    job = container.investigation_jobs.latest_for("INC-2026-0042")
    assert job is not None
    audit = await container.audit.list(incident_id="INC-2026-0042")
    assert len([r for r in audit if r.action == "investigation.start"]) == 1


async def test_a_failing_investigation_is_retried_a_bounded_number_of_times(
    container: Container, monkeypatch: pytest.MonkeyPatch
) -> None:
    incident = await detect(container)
    attempts = 0

    async def boom(incident_id: str, *, actor: Any) -> Any:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("model endpoint unreachable, token=s3cr3t-value")

    monkeypatch.setattr(container.investigations, "investigate", boom)
    clock = container.clock
    assert isinstance(clock, FixedClock)
    for _ in range(6):
        await container.scheduler.tick()
        await settle(container)
        clock.advance(MINUTE)

    assert attempts == 3  # scheduler_auto_investigate_max_attempts
    job = container.investigation_jobs.latest_for(incident.incident_id)
    assert job is not None and job.status is InvestigationJobStatus.FAILED
    assert job.error is not None and "s3cr3t-value" not in job.error and "RuntimeError" in job.error
    audit = await container.audit.list(incident_id=incident.incident_id)
    assert len([r for r in audit if r.action == "investigation.job" and r.outcome == "FAILED"]) == 3
    # A human can still ask for it explicitly.
    submission = await container.investigation_jobs.submit(
        incident.incident_id, requested_by=OPERATOR, trigger=InvestigationTrigger.API
    )
    assert submission.created


async def test_an_orphaned_investigation_is_recovered_but_a_live_one_is_left_alone(container: Container) -> None:
    incident = await detect(container)
    current = await container.incidents.get(incident.incident_id)
    await container.incidents.transition(current, IncidentStatus.INVESTIGATING, actor="test", reason="simulated crash")
    clock = container.clock
    assert isinstance(clock, FixedClock)
    task = AutoInvestigationTask(
        container.incidents, container.investigation_jobs, clock, orphan_after=timedelta(minutes=15)
    )

    await task.run()  # only just started: another process may still be working on it
    await container.investigation_jobs.wait_idle()
    assert (await container.incidents.get(incident.incident_id)).status is IncidentStatus.INVESTIGATING

    clock.advance(timedelta(minutes=16))  # nothing has touched it since
    await task.run()
    await container.investigation_jobs.wait_idle()
    assert (await container.incidents.get(incident.incident_id)).status is IncidentStatus.AWAITING_APPROVAL


async def test_the_catalog_job_and_friends_are_configured_from_settings(tmp_path: Path) -> None:
    settings = make_settings(
        tmp_path,
        scheduler_collect_interval_minutes=2,
        scheduler_sla_sweep_enabled=False,
        scheduler_catalog_refresh_interval_minutes=30,
    )
    c = await build_container(settings, reset=True)
    try:
        jobs = {j.name: j for j in c.scheduler.status().jobs}
        assert set(jobs) == {"collect-detect", "auto-investigate", "sla-sweep", "catalog-refresh"}
        assert jobs["collect-detect"].interval_seconds == 120 and jobs["collect-detect"].enabled
        assert jobs["sla-sweep"].enabled is False
        assert jobs["catalog-refresh"].interval_seconds == 1800
        assert settings.scheduler_enabled is False  # local: off unless asked for
    finally:
        await c.aclose()


# --------------------------------------------------------------------------- job runner behaviour
async def test_submitting_twice_returns_the_same_job_until_it_finishes(
    gated_container: tuple[Container, GatedReasoner],
) -> None:
    c, gate = gated_container
    incident = await detect(c)
    first = await c.investigation_jobs.submit(
        incident.incident_id, requested_by=OPERATOR, trigger=InvestigationTrigger.API
    )
    again = await c.investigation_jobs.submit(
        incident.incident_id, requested_by=SYSTEM_PRINCIPAL, trigger=InvestigationTrigger.SCHEDULER
    )
    assert first.created and not again.created and again.job.job_id == first.job.job_id
    async with asyncio.timeout(10):
        await gate.started.wait()
    assert first.job.status is InvestigationJobStatus.RUNNING and c.investigation_jobs.is_active(incident.incident_id)

    gate.release.set()
    done = await finished(c, first.job.job_id)
    assert done.status is InvestigationJobStatus.SUCCEEDED and done.result is not None
    assert done.result.incident_status is IncidentStatus.AWAITING_APPROVAL and done.result.approval_ids
    assert done.started_at is not None and done.finished_at is not None and done.finished_at >= done.started_at

    third = await c.investigation_jobs.submit(
        incident.incident_id, requested_by=OPERATOR, trigger=InvestigationTrigger.API
    )
    assert third.created and third.job.job_id != first.job.job_id  # a finished job does not block a re-investigation
    await finished(c, third.job.job_id)


async def test_non_investigable_and_unknown_incidents_are_rejected_up_front(container: Container) -> None:
    closed = next(i for i in await container.incidents.repository.list() if i.status is IncidentStatus.CLOSED)
    with pytest.raises(InvestigationNotPossibleError):
        await container.investigation_jobs.submit(
            closed.incident_id, requested_by=OPERATOR, trigger=InvestigationTrigger.API
        )
    with pytest.raises(Exception, match="INC-0000-0000"):
        await container.investigation_jobs.submit(
            "INC-0000-0000", requested_by=OPERATOR, trigger=InvestigationTrigger.API
        )
    assert container.investigation_jobs.active_incident_ids() == set()


async def test_concurrency_is_bounded_and_the_rest_wait_in_the_queue(tmp_path: Path) -> None:
    gate = GatedReasoner()
    c = await build_container(make_settings(tmp_path, investigation_max_concurrency=1), reset=True, reasoner=gate)
    try:
        first = await detect(c)
        other = await c.incidents.get("INC-2026-0042")
        assert other.incident_id == first.incident_id
        runner = c.investigation_jobs
        # Two different incidents: clone the open one under a new id so the queue has something to hold back.
        clone = first.model_copy(update={"incident_id": "INC-2026-0099", "correlation_id": "other-run"})
        await c.incidents.repository.save(clone)
        a = await runner.submit(first.incident_id, requested_by=OPERATOR, trigger=InvestigationTrigger.API)
        b = await runner.submit(clone.incident_id, requested_by=OPERATOR, trigger=InvestigationTrigger.API)
        async with asyncio.timeout(10):
            await gate.started.wait()
        await asyncio.sleep(0.05)
        assert a.job.status is InvestigationJobStatus.RUNNING and b.job.status is InvestigationJobStatus.QUEUED
        gate.release.set()
        assert (await finished(c, a.job.job_id)).status is InvestigationJobStatus.SUCCEEDED
        assert (await finished(c, b.job.job_id)).status is InvestigationJobStatus.SUCCEEDED
    finally:
        gate.release.set()
        await c.investigation_jobs.shutdown()
        await c.aclose()


async def test_an_investigation_that_times_out_fails_cleanly_and_the_incident_can_be_retried(
    gated_container: tuple[Container, GatedReasoner],
) -> None:
    c, gate = gated_container
    incident = await detect(c)
    runner = InvestigationJobRunner(
        c.investigations, c.incidents, c.audit, c.clock, timeout=timedelta(milliseconds=200)
    )
    submission = await runner.submit(incident.incident_id, requested_by=OPERATOR, trigger=InvestigationTrigger.API)
    await runner.wait_idle()
    job = runner.get(submission.job.job_id)
    assert job is not None and job.status is InvestigationJobStatus.FAILED
    assert job.error is not None and job.error.startswith("Timed out")
    # The abandoned investigation must not leave the incident stuck in INVESTIGATING.
    assert (await c.incidents.get(incident.incident_id)).status is IncidentStatus.DETECTED

    gate.release.set()  # a later attempt with a responsive model works
    retry = await c.investigation_jobs.submit(
        incident.incident_id, requested_by=OPERATOR, trigger=InvestigationTrigger.API
    )
    assert (await finished(c, retry.job.job_id)).status is InvestigationJobStatus.SUCCEEDED


async def test_shutdown_cancels_running_investigations_and_puts_incidents_back(
    gated_container: tuple[Container, GatedReasoner],
) -> None:
    c, gate = gated_container
    incident = await detect(c)
    submission = await c.investigation_jobs.submit(
        incident.incident_id, requested_by=OPERATOR, trigger=InvestigationTrigger.API
    )
    async with asyncio.timeout(10):
        await gate.started.wait()
    assert (await c.incidents.get(incident.incident_id)).status is IncidentStatus.INVESTIGATING

    await c.investigation_jobs.shutdown()
    job = c.investigation_jobs.get(submission.job.job_id)
    assert job is not None and job.status is InvestigationJobStatus.FAILED and "shutting down" in (job.error or "")
    assert (await c.incidents.get(incident.incident_id)).status is IncidentStatus.DETECTED
    with pytest.raises(InvestigationNotPossibleError, match="shutting down"):
        await c.investigation_jobs.submit(incident.incident_id, requested_by=OPERATOR, trigger=InvestigationTrigger.API)


# ------------------------------------------------------------------- AC3: HTTP is never blocked
async def test_a_long_investigation_never_blocks_http_requests(
    gated_container: tuple[Container, GatedReasoner],
) -> None:
    c, gate = gated_container
    incident = await detect(c)
    app = create_app(container=c)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http,
    ):
        # The reasoner blocks until released, so a synchronous implementation would hang here forever.
        accepted = await asyncio.wait_for(
            http.post(f"/incidents/{incident.incident_id}/investigate", headers=VIEWER), 5
        )
        assert accepted.status_code == 202
        body = accepted.json()
        assert body["status"] in {"QUEUED", "RUNNING"} and body["already_active"] is False
        assert accepted.headers["location"] == body["status_url"] == f"/investigations/{body['job_id']}"
        async with asyncio.timeout(10):
            await gate.started.wait()  # the investigation really is in flight now

        # Everything else keeps answering while it runs.
        assert (await asyncio.wait_for(http.get("/health"), 5)).status_code == 200
        assert (await asyncio.wait_for(http.get("/health/ready"), 5)).status_code == 200
        listing = await asyncio.wait_for(http.get("/incidents", headers=VIEWER), 5)
        assert listing.status_code == 200 and listing.json()
        running = (await asyncio.wait_for(http.get(body["status_url"], headers=VIEWER), 5)).json()
        assert running["status"] == "RUNNING" and running["started_at"] and running["result"] is None
        latest = (await http.get(f"/incidents/{incident.incident_id}/investigation", headers=VIEWER)).json()
        assert latest["job_id"] == body["job_id"]

        # A duplicate request returns the existing job instead of starting a second investigation.
        duplicate = await asyncio.wait_for(
            http.post(f"/incidents/{incident.incident_id}/investigate", headers=VIEWER), 5
        )
        assert duplicate.status_code == 202
        assert duplicate.json()["job_id"] == body["job_id"] and duplicate.json()["already_active"] is True

        gate.release.set()

        async def fetch_job() -> dict[str, Any]:
            polled: dict[str, Any] = (await http.get(body["status_url"], headers=VIEWER)).json()
            return polled

        job = await poll(fetch_job, lambda j: j["status"] not in {"QUEUED", "RUNNING"})
        assert job["status"] == "SUCCEEDED"
        assert job["result"]["incident_status"] == "AWAITING_APPROVAL" and job["result"]["approval_ids"]
        assert job["result"]["trace_id"] and job["result"]["findings"] >= 1

        audit = await c.audit.list(incident_id=incident.incident_id)
        assert len([r for r in audit if r.action == "investigation.start"]) == 1  # one investigation, not two


async def test_investigation_endpoints_report_errors_as_before(container: Container) -> None:
    closed = next(i for i in await container.incidents.repository.list() if i.status is IncidentStatus.CLOSED)
    app = create_app(container=container)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http,
    ):
        unknown = await http.post("/incidents/INC-0000-0000/investigate", headers=VIEWER)
        assert unknown.status_code == 404 and unknown.json()["error"] == "INCIDENT_NOT_FOUND"
        not_possible = await http.post(f"/incidents/{closed.incident_id}/investigate", headers=VIEWER)
        assert not_possible.status_code == 409 and not_possible.json()["error"] == "INVESTIGATION_NOT_POSSIBLE"
        no_job = await http.get("/investigations/INV-nope", headers=VIEWER)
        assert no_job.status_code == 404 and no_job.json()["error"] == "INVESTIGATION_NOT_FOUND"
        never_investigated = await http.get(f"/incidents/{closed.incident_id}/investigation", headers=VIEWER)
        assert never_investigated.status_code == 404
        missing_incident = await http.get("/incidents/INC-0000-0000/investigation", headers=VIEWER)
        assert missing_incident.status_code == 404 and missing_incident.json()["error"] == "INCIDENT_NOT_FOUND"


# ------------------------------------------------------------ the whole thing through the app lifespan
async def test_the_app_runs_autonomously_when_the_scheduler_is_enabled(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, scheduler_enabled=True, scheduler_tick_seconds=0.05)
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http,
    ):
        ready = await http.get("/health/ready")
        assert ready.status_code == 200

        async def fetch_incident() -> dict[str, Any]:
            polled: dict[str, Any] = (await http.get("/incidents/INC-2026-0042", headers=VIEWER)).json()
            return polled

        # Nobody calls the API: the scheduler investigates the seeded incident by itself.
        await poll(fetch_incident, lambda i: i["status"] == "AWAITING_APPROVAL", interval=0.05)
        assert (await http.get("/health/ready")).json() == {"status": "ready", "scheduler": "leader"}
        job = (await http.get("/incidents/INC-2026-0042/investigation", headers=VIEWER)).json()
        assert job["trigger"] == "SCHEDULER" and job["requested_by"] == "system:orqentis"
    # Shutdown released the lease and stopped the scheduler.
    runtime = app.state.runtime
    assert runtime.container is None
