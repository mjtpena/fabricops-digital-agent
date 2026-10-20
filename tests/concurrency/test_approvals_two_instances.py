"""Approvals on PostgreSQL: decisions and executions are atomic across separate application instances.

The first half re-runs the existing approval concurrency scenarios
(tests/scenarios/test_approval_concurrency.py) with the fixture swapped for a container on PostgreSQL. The second
half builds two complete containers, each with its own connection pool, locks and write client, which share nothing
but the database.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from tests.conftest import ADMIN, OPERATOR, detect_and_investigate
from tests.scenarios import test_approval_concurrency as base
from tests.scenarios.test_approval_concurrency import SlowSpy, spy  # noqa: F401 (fixture used by name)
from tests.support.postgres_app import AppStorage

from orqentis.actions.gateway import ActionBlockedError, ActionOutcome, GatewayStatus
from orqentis.approvals.service import ApprovalStateError
from orqentis.bootstrap import Container, build_container
from orqentis.domain.actions import ActionStatus
from orqentis.domain.approvals import ApprovalRequest, ApprovalStatus

pytestmark = pytest.mark.security


@pytest.fixture
async def armed(app_storage: AppStorage, tmp_path: Path, spy: SlowSpy) -> AsyncIterator[Container]:  # noqa: F811
    """The scenario fixture of the local suite, on PostgreSQL."""
    c = await build_container(app_storage.settings(tmp_path, enable_write_actions=True), write_client=spy)
    try:
        yield c
    finally:
        await c.investigation_jobs.shutdown()
        await c.aclose()


# Every scenario of the local suite that runs on the shared ``armed`` container also runs here.
for _name, _test in list(vars(base).items()):
    if _name.startswith("test_") and "armed" in inspect.signature(_test).parameters:
        globals()[_name] = _test


async def test_n_concurrent_executes_across_two_instances_make_exactly_one_write(
    app_storage: AppStorage, tmp_path: Path
) -> None:
    spy_a, spy_b = SlowSpy(), SlowSpy()
    first = await build_container(app_storage.settings(tmp_path / "a", enable_write_actions=True), write_client=spy_a)
    second = await build_container(app_storage.settings(tmp_path / "b", enable_write_actions=True), write_client=spy_b)
    try:
        incident = await detect_and_investigate(first)
        approval_id = incident.approval_ids[0]
        await first.approvals.decide(approval_id, principal=ADMIN, approve=True)

        gateways = [first.gateway, second.gateway]
        results = await asyncio.gather(
            *(gateways[n % 2].execute(approval_id, principal=ADMIN) for n in range(40)), return_exceptions=True
        )
        done = [r for r in results if isinstance(r, ActionOutcome)]
        blocked = [r for r in results if isinstance(r, ActionBlockedError)]
        assert len(done) == 1 and done[0].status is GatewayStatus.EXECUTED
        assert len(blocked) == 39 and {b.code for b in blocked} == {"ALREADY_EXECUTED"}
        assert len(spy_a.calls) + len(spy_b.calls) == 1  # one write in the whole system
        for instance in (first, second):
            stored = await instance.approvals.get(approval_id)
            assert stored.proposed_action.status is ActionStatus.SUCCEEDED
        assert (await first.audit.verify()).valid
        executed = [r for r in await second.audit.list() if r.action == "action.execute"]
        assert [r.outcome for r in executed].count("SUCCEEDED") == 1
    finally:
        for instance in (first, second):
            await instance.investigation_jobs.shutdown()
            await instance.aclose()


async def test_an_approve_and_a_reject_racing_on_two_instances_store_exactly_one_decision(
    app_storage: AppStorage, tmp_path: Path
) -> None:
    first = await build_container(app_storage.settings(tmp_path / "a", enable_write_actions=True))
    second = await build_container(app_storage.settings(tmp_path / "b", enable_write_actions=True))
    try:
        incident = await detect_and_investigate(first)
        approval_id = incident.approval_ids[0]
        results = await asyncio.gather(
            first.approvals.decide(approval_id, principal=OPERATOR, approve=True),
            second.approvals.decide(approval_id, principal=ADMIN, approve=False),
            return_exceptions=True,
        )
        winners = [r for r in results if isinstance(r, ApprovalRequest)]
        losers = [r for r in results if isinstance(r, ApprovalStateError)]
        assert len(winners) == 1 and len(losers) == 1
        stored = await second.approvals.get(approval_id)
        assert stored.status is winners[0].status
        assert stored.status in {ApprovalStatus.APPROVED, ApprovalStatus.REJECTED}
        assert stored.revision == 1
    finally:
        for instance in (first, second):
            await instance.investigation_jobs.shutdown()
            await instance.aclose()
