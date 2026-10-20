"""The application end to end on the PostgreSQL storage backend (PostgreSQL + Azurite containers).

Everything but the Blob credential is the production composition: ``build_container`` with ``storage_backend=postgres``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from orqentis.api.app import create_app
from orqentis.bootstrap import Container, build_container
from orqentis.config.clock import FixedClock
from orqentis.domain.approvals import ApprovalStatus
from orqentis.domain.incidents import IncidentStatus
from orqentis.observability.anchors import AuditAnchorTask
from orqentis.storage.postgres import PostgresAuditTrail
from tests.conftest import ADMIN, OPERATOR, detect_and_investigate
from tests.support.polling import wait_until
from tests.support.postgres_app import AppStorage


@pytest.fixture
async def container(app_storage: AppStorage, tmp_path: Path) -> AsyncIterator[Container]:
    c = await build_container(app_storage.settings(tmp_path / "a"))
    try:
        yield c
    finally:
        await c.investigation_jobs.shutdown()
        await c.aclose()


async def test_the_incident_flow_runs_on_postgres_and_blob_and_is_visible_to_a_second_instance(
    container: Container, app_storage: AppStorage, tmp_path: Path
) -> None:
    incident = await detect_and_investigate(container)
    assert incident.status is IncidentStatus.AWAITING_APPROVAL and incident.approval_ids and incident.report_uri
    approval = await container.approvals.decide(incident.approval_ids[0], principal=ADMIN, approve=True)
    assert approval.status is ApprovalStatus.APPROVED

    # Evidence and the report are Blob objects, not files.
    uris = await container.evidence_store.list(incident.incident_id)
    assert any(u.endswith("report.md") for u in uris) and any(u.endswith("EV001.json") for u in uris)
    assert container.evidence_store.local_path(incident.report_uri) is None
    assert not (container.settings.data_dir / "evidence").exists()
    assert not container.settings.state_db_path.exists() and not container.settings.audit_log_path.exists()

    # A second instance on the same database sees all of it.
    other = await build_container(app_storage.settings(tmp_path / "b"))
    try:
        seen = await other.incidents.get(incident.incident_id)
        assert seen.status is IncidentStatus.AWAITING_APPROVAL
        assert (await other.approvals.get(approval.approval_id)).status is ApprovalStatus.APPROVED
        assert await other.evidence_store.get_text(incident.report_uri)
        assert await other.event_store.count() == await container.event_store.count() > 0
        assert (await other.audit.verify()).valid
        assert [r.sequence for r in await other.audit.list()] == [r.sequence for r in await container.audit.list()]
    finally:
        await other.investigation_jobs.shutdown()
        await other.aclose()


async def test_the_audit_chain_is_anchored_and_verified_against_the_anchor(container: Container) -> None:
    await detect_and_investigate(container)
    assert isinstance(container.audit, PostgresAuditTrail)
    assert "audit-anchor" in {j.name for j in container.scheduler.status().jobs}
    head = await container.audit.head()
    assert head.sequence > 3

    # Run the scheduled anchoring job exactly as the scheduler would (it is leader-only; no wait for a tick here).
    anchors = container.audit._anchors
    assert anchors is not None
    task = AuditAnchorTask(container.audit, anchors, container.clock, epochs=container.audit._epochs)
    anchor = await task.run()
    assert anchor is not None and anchor.sequence == head.sequence and anchor.hash == head.hash
    assert await task.run() is None  # nothing new to anchor
    assert (await container.audit.verify()).valid
    assert container.audit_anchoring is not None and not container.audit_anchoring.stale


async def test_a_job_submitted_through_the_runner_is_durable_and_visible_by_id(container: Container) -> None:
    from orqentis.runtime.investigations import InvestigationJobStatus, InvestigationTrigger

    _, created = await container.collect_and_detect()
    incident = created[0]
    submission = await container.investigation_jobs.submit(
        incident.incident_id, requested_by=OPERATOR, trigger=InvestigationTrigger.API
    )
    assert submission.created
    await container.investigation_jobs.wait_idle()
    job = await container.investigation_jobs.get(submission.job.job_id)
    assert job is not None and job.status is InvestigationJobStatus.SUCCEEDED and job.requested_by == OPERATOR.id
    again = await container.investigation_jobs.latest_for(incident.incident_id)
    assert again is not None and again.job_id == job.job_id


async def test_a_leader_whose_audit_anchoring_has_stopped_says_so_in_its_readiness_and_stays_ready(
    app_storage: AppStorage, tmp_path: Path
) -> None:
    """Anchoring is what makes a rewrite of the database detectable; the leader runs it and must say when it has not
    worked for twice its interval. A follower does not run it, so it has nothing to report."""
    settings = app_storage.settings(
        tmp_path / "leader",
        scheduler_enabled=True,
        scheduler_tick_seconds=0.05,
        scheduler_audit_anchor_enabled=False,  # the job that would keep it fresh does not run: it has stopped working
    )
    follower = await build_container(app_storage.settings(tmp_path / "follower"))
    app = create_app(settings)
    try:
        async with app.router.lifespan_context(app):
            leader = app.state.runtime.container
            assert leader is not None and leader.audit_anchoring is not None
            await wait_until(lambda: leader.scheduler.is_leader, within=30)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:  # type: ignore[arg-type]
                fresh = await http.get("/health/ready")
                assert fresh.status_code == 200 and fresh.json() == {"status": "ready", "scheduler": "leader"}

                assert isinstance(leader.clock, FixedClock) and isinstance(follower.clock, FixedClock)
                leader.clock.advance(timedelta(minutes=31))  # more than twice the 15-minute anchor interval
                follower.clock.advance(timedelta(minutes=31))
                await wait_until(lambda: leader.scheduler.is_leader, within=30)
                stale = await http.get("/health/ready")
                assert stale.status_code == 200  # still ready: the service keeps working, but it says what is wrong
                assert stale.json() == {"status": "ready", "scheduler": "leader", "degraded": ["audit anchoring stale"]}

                # A run that works (even one with nothing new to anchor) clears it.
                await leader.audit.record(actor="test", action="test.append", outcome="OK")  # something to anchor
                assert await leader.audit_anchoring.run() is not None
                recovered = await http.get("/health/ready")
                assert recovered.json() == {"status": "ready", "scheduler": "leader"}

            # The follower's own anchoring is as stale, and it does not report it: it is not the one that anchors.
            assert follower.audit_anchoring is not None and follower.audit_anchoring.stale
            follower_app = create_app(follower.settings, container=follower)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=follower_app),  # type: ignore[arg-type]
                base_url="http://test",
            ) as http:
                body = (await http.get("/health/ready")).json()
            assert "degraded" not in body
    finally:
        await follower.investigation_jobs.shutdown()
        await follower.aclose()
