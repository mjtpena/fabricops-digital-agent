"""Two application instances as separate OS processes against one PostgreSQL (and Azurite).

They share nothing in memory: no lock, no cache, no event loop. They even start (and migrate) at the same moment.
"""

from __future__ import annotations

from itertools import pairwise
from pathlib import Path

import asyncpg
import pytest
from tests.conftest import ADMIN, detect_and_investigate
from tests.support.postgres_app import AppStorage
from tests.support.processes import Instance, instance_args

from orqentis.bootstrap import build_container
from orqentis.domain.actions import ActionStatus

pytestmark = pytest.mark.security


async def start_pair(command: str, storage: AppStorage, tmp_path: Path, **extra: object) -> list[Instance]:
    instances = [
        await Instance.start(command, instance_args(storage, storage.settings(tmp_path / name), **extra))
        for name in ("one", "two")
    ]
    try:
        for instance in instances:
            await instance.wait_for("READY")
    except BaseException:
        for instance in instances:
            await instance.kill()
        raise
    return instances


async def test_two_processes_that_start_together_migrate_once_and_write_one_audit_chain(
    app_storage: AppStorage, tmp_path: Path
) -> None:
    instances = await start_pair("audit-load", app_storage, tmp_path, count=60)
    for instance in instances:
        await instance.go()  # both start appending at the same moment
    results = [await instance.result() for instance in instances]

    assert results[0]["pid"] != results[1]["pid"]
    sequences = sorted(s for r in results for s in r["sequences"])
    assert sequences == list(range(1, 121))  # 120 records, no sequence number twice, none missing

    checker = await build_container(app_storage.settings(tmp_path / "check"))
    try:
        chain = await checker.audit.list()
        assert [r.sequence for r in chain] == list(range(1, 121))
        for before, after in pairwise(chain):
            assert after.prev_hash == before.hash  # one linear chain although two processes wrote it
        verification = await checker.audit.verify()
        assert verification.valid and verification.records == 120
        assert len({r.actor for r in chain}) == 2
    finally:
        await checker.aclose()


async def test_n_concurrent_executes_across_two_processes_make_exactly_one_write(
    app_storage: AppStorage, tmp_path: Path
) -> None:
    prepare = await build_container(app_storage.settings(tmp_path / "prepare", enable_write_actions=True))
    try:
        incident = await detect_and_investigate(prepare)
        approval_id = incident.approval_ids[0]
        await prepare.approvals.decide(approval_id, principal=ADMIN, approve=True)
        await prepare.audit.verify()
    finally:
        await prepare.investigation_jobs.shutdown()
        await prepare.aclose()
    conn = await asyncpg.connect(
        host=app_storage.server.host,
        port=app_storage.server.port,
        user=app_storage.server.user,
        database=app_storage.server.database,
    )
    try:
        await conn.execute(
            f'CREATE TABLE "{app_storage.schema}".test_writes (pid int, approval_id text, at timestamptz DEFAULT now())'
        )

        instances = []
        for name in ("one", "two"):
            settings = app_storage.settings(tmp_path / name, enable_write_actions=True)
            instances.append(
                await Instance.start(
                    "execute-load", instance_args(app_storage, settings, approval_id=approval_id, count=15)
                )
            )
        for instance in instances:
            await instance.wait_for("READY")
        for instance in instances:
            await instance.go()
        results = [await instance.result() for instance in instances]

        assert sum(r["executed"] for r in results) == 1  # one process won the claim ...
        refused = [c for r in results for c in r["blocked"]]
        assert refused == ["ALREADY_EXECUTED"] * 29  # ... and the 29 other attempts were refused
        assert all(r["other"] == [] for r in results)
        rows = await conn.fetch(f'SELECT pid, approval_id FROM "{app_storage.schema}".test_writes')  # noqa: S608
        assert len(rows) == 1 and rows[0]["approval_id"] == approval_id  # one write, in the whole system
    finally:
        await conn.close()

    checker = await build_container(app_storage.settings(tmp_path / "check"))
    try:
        stored = await checker.approvals.get(approval_id)
        assert stored.proposed_action.status is ActionStatus.SUCCEEDED
        trail = await checker.audit.list()
        executions = [r for r in trail if r.action == "action.execute" and r.outcome == "SUCCEEDED"]
        assert len(executions) == 1
        assert (await checker.audit.verify()).valid
    finally:
        await checker.aclose()
