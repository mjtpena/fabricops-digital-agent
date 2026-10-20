"""Stopping an instance mid-job and starting another loses no events, jobs or audit records.

* A graceful stop (a deploy: the old revision receives SIGTERM) hands the running job back to the queue at once.
* A hard stop (the process is killed, the node is lost) leaves a claim that expires; another instance re-claims the job.
Either way a *different* instance finishes the same job, the incident ends up investigated, and nothing was lost.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import asyncpg
import httpx
import pytest
from tests.conftest import OPERATOR, detect
from tests.support.polling import poll
from tests.support.postgres_app import AppStorage
from tests.support.processes import Instance, instance_args
from tests.support.reasoners import GatedReasoner

from orqentis.api.app import create_app
from orqentis.bootstrap import Container, build_container
from orqentis.domain.incidents import IncidentStatus
from orqentis.runtime.investigations import InvestigationJobStatus, InvestigationTrigger

pytestmark = pytest.mark.security

# The smallest claim lifetime the settings allow: a killed instance's job is re-claimed within about this many seconds.
FAST_JOBS = {"investigation_claim_ttl_seconds": 10, "investigation_poll_interval_seconds": 1}


async def finished(c: Container, job_id: str, *, within: float = 90.0):
    return await poll(
        lambda: c.investigation_jobs.get(job_id),
        lambda j: j is not None and not j.status.is_active,
        within=within,
        interval=0.2,
    )


async def audit_ids(c: Container) -> list[str]:
    return [r.audit_id for r in await c.audit.list()]


async def test_a_killed_instance_mid_job_loses_nothing_and_another_instance_finishes_the_job(
    app_storage: AppStorage, tmp_path: Path
) -> None:
    survivor = await build_container(app_storage.settings(tmp_path / "survivor", **FAST_JOBS))
    try:
        incident = await detect(survivor)
        victim = await Instance.start(
            "hang-investigation",
            instance_args(
                app_storage,
                app_storage.settings(tmp_path / "victim", **FAST_JOBS),
                incident_id=incident.incident_id,
            ),
        )
        try:
            started = json.loads((await victim.wait_for("RUNNING")).removeprefix("RUNNING "))
            job_id = started["job_id"]
            running = await survivor.investigation_jobs.get(job_id)
            assert running is not None and running.status is InvestigationJobStatus.RUNNING  # seen from here
            assert (await survivor.incidents.get(incident.incident_id)).status is IncidentStatus.INVESTIGATING
            events_before = await survivor.event_store.count()
            audit_before = await audit_ids(survivor)
            assert events_before > 0 and audit_before
        finally:
            await victim.kill()  # no shutdown hook, no lease release, no job hand-back: the process is simply gone

        await survivor.investigation_jobs.start()  # the surviving instance polls for work
        job = await finished(survivor, job_id)

        assert job is not None and job.job_id == job_id and job.status is InvestigationJobStatus.SUCCEEDED
        assert job.result is not None and job.result.incident_status is IncidentStatus.AWAITING_APPROVAL
        after = await survivor.incidents.get(incident.incident_id)
        assert after.status is IncidentStatus.AWAITING_APPROVAL and after.approval_ids
        assert await survivor.event_store.count() >= events_before  # no event was lost
        assert (await audit_ids(survivor))[: len(audit_before)] == audit_before  # no audit record was lost or reordered
        assert (await survivor.audit.verify()).valid
        trail = await survivor.audit.list(incident_id=incident.incident_id)
        assert len([r for r in trail if r.action == "investigation.start"]) == 2  # the victim began it, then this one
        # Exactly one job: the same one, claimed twice. Nothing duplicated, nothing orphaned.
        conn = await asyncpg.connect(
            host=app_storage.server.host, port=app_storage.server.port, user=app_storage.server.user,
            database=app_storage.server.database,
        )  # fmt: skip
        try:
            rows = await conn.fetch(
                f'SELECT job_id, status, attempts, claim_seq FROM "{app_storage.schema}".investigation_jobs'  # noqa: S608
            )
        finally:
            await conn.close()
        summary = [(r["job_id"], r["status"], r["attempts"], r["claim_seq"]) for r in rows]
        assert summary == [(job_id, "SUCCEEDED", 1, 2)]
    finally:
        await survivor.investigation_jobs.shutdown()
        await survivor.aclose()


async def test_a_graceful_stop_hands_the_running_job_to_the_next_instance_at_once(
    app_storage: AppStorage, tmp_path: Path
) -> None:
    gate = GatedReasoner()
    old = await build_container(app_storage.settings(tmp_path / "old", **FAST_JOBS), reasoner=gate)
    new = await build_container(app_storage.settings(tmp_path / "new", **FAST_JOBS))
    try:
        await old.investigation_jobs.start()
        incident = await detect(old)
        submission = await old.investigation_jobs.submit(
            incident.incident_id, requested_by=OPERATOR, trigger=InvestigationTrigger.API
        )
        async with asyncio.timeout(30):
            await gate.started.wait()
        audit_before = await audit_ids(old)

        # The old revision is told to stop (Runtime.stop): its job goes back to the queue, its incident to DETECTED.
        await old.investigation_jobs.shutdown()
        queued = await new.investigation_jobs.get(submission.job.job_id)
        assert queued is not None and queued.status is InvestigationJobStatus.QUEUED
        assert (await new.incidents.get(incident.incident_id)).status is IncidentStatus.DETECTED
        gate.release.set()
        await old.aclose()

        await new.investigation_jobs.start()  # the new revision claims it within one poll
        job = await finished(new, submission.job.job_id, within=30)
        assert job is not None and job.status is InvestigationJobStatus.SUCCEEDED
        assert (await new.incidents.get(incident.incident_id)).status is IncidentStatus.AWAITING_APPROVAL
        assert (await audit_ids(new))[: len(audit_before)] == audit_before
        assert (await new.audit.verify()).valid
        # A graceful stop is not a failed attempt: the incident keeps its whole retry budget.
        assert await new.investigation_jobs.failed_attempts(incident.incident_id) == 0
    finally:
        gate.release.set()
        await new.investigation_jobs.shutdown()
        await new.aclose()


async def test_a_job_submitted_on_one_replica_is_visible_on_the_other_and_runs_exactly_once(
    app_storage: AppStorage, tmp_path: Path
) -> None:
    first = await build_container(app_storage.settings(tmp_path / "a", **FAST_JOBS))
    second = await build_container(app_storage.settings(tmp_path / "b", **FAST_JOBS))
    headers = {"X-Orqentis-User": "olivia@contoso.com", "X-Orqentis-Roles": "FabricOps.Operator"}
    app_a, app_b = create_app(container=first), create_app(container=second)
    try:
        incident = await detect(first)
        await first.investigation_jobs.start()
        await second.investigation_jobs.start()
        async with (
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app_a), base_url="http://a") as http_a,
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app_b), base_url="http://b") as http_b,
        ):
            accepted, duplicate = await asyncio.gather(
                http_a.post(f"/incidents/{incident.incident_id}/investigate", headers=headers),
                http_b.post(f"/incidents/{incident.incident_id}/investigate", headers=headers),
            )
            assert accepted.status_code == duplicate.status_code == 202
            assert accepted.json()["job_id"] == duplicate.json()["job_id"]  # one job, whichever replica got the request
            assert sorted([accepted.json()["already_active"], duplicate.json()["already_active"]]) == [False, True]
            job_id = accepted.json()["job_id"]

            async def status() -> dict[str, object]:
                polled: dict[str, object] = (await http_b.get(f"/investigations/{job_id}", headers=headers)).json()
                return polled

            done = await poll(status, lambda j: j["status"] not in {"QUEUED", "RUNNING"}, within=60, interval=0.2)
            assert done["status"] == "SUCCEEDED"
            latest = (await http_a.get(f"/incidents/{incident.incident_id}/investigation", headers=headers)).json()
            assert latest["job_id"] == job_id and latest["status"] == "SUCCEEDED"
        trail = await first.audit.list(incident_id=incident.incident_id)
        starts = [r for r in trail if r.action == "investigation.start"]
        assert len(starts) == 1  # the investigation ran once, on one of the two replicas
    finally:
        for instance in (first, second):
            await instance.investigation_jobs.shutdown()
            await instance.aclose()
