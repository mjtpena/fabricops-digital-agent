"""A Fabric write is never made without the intent to make it being on the audit chain (the approval boundary).

Reproduced against the first version: when the audit append failed after the write (a stalled writer holding the chain
head, pool exhaustion, a transient error), one write had reached Fabric and the chain had no ``action.execute`` record.
Now the intent is appended first; if that fails nothing is written, and if only the outcome record fails the intent
record still names the write.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from orqentis.actions.gateway import ActionBlockedError, AuditUnavailableError
from orqentis.bootstrap import Container, build_container
from orqentis.domain.actions import Action, ActionStatus
from orqentis.domain.approvals import ApprovalRequest
from tests.conftest import ADMIN, OPERATOR, detect_and_investigate, make_settings

pytestmark = pytest.mark.security


class SpyWriteClient:
    """Records every write and what the audit trail already held at that moment."""

    def __init__(self) -> None:
        self.calls: list[tuple[Action, ApprovalRequest]] = []
        self.audit_actions_seen_by_the_write: list[list[str]] = []
        self.container: Container | None = None

    async def execute(self, action: Action, *, approval: ApprovalRequest) -> dict[str, Any]:
        assert self.container is not None
        self.audit_actions_seen_by_the_write.append([r.action for r in await self.container.audit.list()])
        self.calls.append((action, approval))
        return {"executed": True}


@pytest.fixture
def spy() -> SpyWriteClient:
    return SpyWriteClient()


@pytest.fixture
async def armed(tmp_path: Path, spy: SpyWriteClient) -> AsyncIterator[Container]:
    c = await build_container(make_settings(tmp_path, enable_write_actions=True), reset=True, write_client=spy)
    spy.container = c
    try:
        yield c
    finally:
        await c.aclose()


def failing_for(
    container: Container, monkeypatch: pytest.MonkeyPatch, *, action: str, outcomes: set[str] | None
) -> None:
    """Make the audit trail refuse one kind of record (an unavailable audit chain)."""
    real = container.audit.record

    async def record(**kwargs: Any) -> Any:
        if kwargs.get("action") == action and (outcomes is None or kwargs.get("outcome") in outcomes):
            raise ConnectionError("the audit chain is unavailable")
        return await real(**kwargs)

    monkeypatch.setattr(container.audit, "record", record)


async def approved(armed: Container) -> str:
    incident = await detect_and_investigate(armed)
    approval_id = incident.approval_ids[0]
    await armed.approvals.decide(approval_id, principal=ADMIN, approve=True)
    return approval_id


async def test_the_intent_is_on_the_chain_before_the_write_reaches_fabric(
    armed: Container, spy: SpyWriteClient
) -> None:
    approval_id = await approved(armed)
    outcome = await armed.gateway.execute(approval_id, principal=OPERATOR)
    assert outcome.action.status is ActionStatus.SUCCEEDED and len(spy.calls) == 1
    # What the audit trail held at the moment the write was made: the intent, and not yet the outcome.
    seen = spy.audit_actions_seen_by_the_write[0]
    assert "action.execute.intent" in seen and "action.execute" not in seen
    records = await armed.audit.list()
    intent = next(r for r in records if r.action == "action.execute.intent")
    done = next(r for r in records if r.action == "action.execute")
    assert intent.sequence < done.sequence and done.outcome == "SUCCEEDED"
    approval = await armed.approvals.get(approval_id)
    assert intent.details["approval_id"] == approval_id
    assert intent.details["fingerprint"] == approval.action_fingerprint
    assert intent.details["claim_revision"] == approval.revision - 1  # the claim, before the outcome transition


async def test_when_the_intent_cannot_be_recorded_nothing_is_written_and_the_approval_is_used_up(
    armed: Container, spy: SpyWriteClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    approval_id = await approved(armed)
    failing_for(armed, monkeypatch, action="action.execute.intent", outcomes=None)

    with pytest.raises(AuditUnavailableError) as raised:
        await armed.gateway.execute(approval_id, principal=OPERATOR)
    assert raised.value.code == "AUDIT_UNAVAILABLE"
    assert spy.calls == []  # fail closed: no record, no write

    approval = await armed.approvals.get(approval_id)
    assert approval.proposed_action.status is ActionStatus.FAILED  # not left looking like a write in flight
    assert (
        approval.proposed_action.result is not None and approval.proposed_action.result["code"] == "AUDIT_UNAVAILABLE"
    )
    monkeypatch.undo()
    with pytest.raises(ActionBlockedError) as again:  # a retry does not write either: the approval is spent
        await armed.gateway.execute(approval_id, principal=OPERATOR)
    assert again.value.code == "ALREADY_EXECUTED" and spy.calls == []
    assert (await armed.audit.verify()).valid


async def test_when_only_the_outcome_cannot_be_recorded_the_write_stands_and_the_intent_names_it(
    armed: Container, spy: SpyWriteClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    approval_id = await approved(armed)
    failing_for(armed, monkeypatch, action="action.execute", outcomes={"SUCCEEDED", "FAILED"})

    with caplog.at_level(logging.ERROR):
        outcome = await armed.gateway.execute(approval_id, principal=OPERATOR)
    assert outcome.action.status is ActionStatus.SUCCEEDED and len(spy.calls) == 1  # one write, reported as done
    assert "intent record stands" in caplog.text

    monkeypatch.undo()
    records = await armed.audit.list()
    assert [r.action for r in records if r.action.startswith("action.execute")] == ["action.execute.intent"]
    approval = await armed.approvals.get(approval_id)
    assert approval.proposed_action.status is ActionStatus.SUCCEEDED  # the approval says what happened
    assert approval.revision >= 2
    assert (await armed.audit.verify()).valid
