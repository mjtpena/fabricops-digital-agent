"""A notification is published only after what it announces is stored (ADR-0018, section 1).

If the announcement came first, a failing save would leave people told about something that never happened, and the page
the link opens would show the old state.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from orqentis.bootstrap import Container, build_container
from orqentis.config.clock import FixedClock
from orqentis.domain.approvals import ApprovalRequest
from orqentis.domain.incidents import Incident
from orqentis.domain.taxonomy import Severity
from orqentis.notifications.model import NotificationEvent, NotificationKind
from tests.conftest import ADMIN, OPERATOR, detect, make_settings
from tests.support.notifications import RecordingSink

Log = list[tuple[str, str]]


class OrderedSink(RecordingSink):
    """Records every publication in the same log as the writes, so the order can be compared."""

    def __init__(self, log: Log) -> None:
        super().__init__()
        self._log = log

    def publish(self, event: NotificationEvent) -> None:
        super().publish(event)
        self._log.append(("publish", event.kind.value))


@pytest.fixture
async def watched(tmp_path: Path) -> AsyncIterator[tuple[Container, Log, OrderedSink]]:
    """A container whose repository and approval store log each write, and whose services publish to ``OrderedSink``."""
    container = await build_container(make_settings(tmp_path, enable_write_actions=True), reset=True)
    log: Log = []
    sink = OrderedSink(log)
    container.incidents._notifications = sink  # type: ignore[attr-defined]
    container.approvals._notifications = sink  # type: ignore[attr-defined]
    container.gateway._notifications = sink  # type: ignore[attr-defined]

    repository = container.incidents.repository
    real_save = repository.save

    async def save(incident: Incident) -> None:
        await real_save(incident)
        log.append(("save-incident", incident.severity.value))

    repository.save = save  # type: ignore[method-assign]

    provider = container.approvals.provider
    real_submit, real_transition = provider.submit, provider.transition

    async def submit(approval: ApprovalRequest) -> Any:
        result = await real_submit(approval)
        log.append(("store-approval", approval.status.value))
        return result

    async def transition(updated: ApprovalRequest, *, expected_revision: int) -> Any:
        result = await real_transition(updated, expected_revision=expected_revision)
        if result is not None:
            log.append(("transition-approval", updated.proposed_action.status.value))
        return result

    provider.submit = submit  # type: ignore[method-assign]
    provider.transition = transition  # type: ignore[method-assign]
    try:
        yield container, log, sink
    finally:
        await container.aclose()


def before(log: Log, stored: tuple[str, str], published: NotificationKind) -> bool:
    """Whether ``stored`` is in the log, and the first ``published`` comes after the last matching ``stored``."""
    announced = log.index(("publish", published.value))
    return any(entry == stored for entry in log[:announced])


async def test_an_incident_is_announced_after_it_is_stored(watched: tuple[Container, Log, OrderedSink]) -> None:
    container, log, _ = watched
    incident = await detect(container)
    assert before(log, ("save-incident", incident.severity.value), NotificationKind.INCIDENT_OPENED)


async def test_an_approval_request_is_announced_after_it_is_stored(watched: tuple[Container, Log, OrderedSink]) -> None:
    container, log, _ = watched
    incident = await detect(container)
    await container.investigations.investigate(incident.incident_id, actor=OPERATOR)
    first_request = log.index(("publish", NotificationKind.APPROVAL_REQUESTED.value))
    assert ("store-approval", "PENDING") in log[:first_request]
    assert (
        log[first_request - 1][0] != "publish"
    )  # the entry just before it is the write (or its audit), not an earlier news item


async def test_an_escalation_is_announced_only_after_the_new_severity_is_stored(
    watched: tuple[Container, Log, OrderedSink],
) -> None:
    """Regression: ``raise_severity`` published first and left the caller to save: a failing save was announced."""
    container, log, sink = watched
    incident = await detect(container)
    clock = container.clock
    assert isinstance(clock, FixedClock)
    clock.advance(timedelta(hours=1))
    await container.investigations.investigate(incident.incident_id, actor=OPERATOR)

    assert before(log, ("save-incident", Severity.CRITICAL.value), NotificationKind.INCIDENT_ESCALATED)
    stored = await container.incidents.get(incident.incident_id)
    assert stored.severity is Severity.CRITICAL
    assert [e.kind for e in sink.events].count(NotificationKind.INCIDENT_ESCALATED) == 1


async def test_a_failed_save_means_no_escalation_is_announced(watched: tuple[Container, Log, OrderedSink]) -> None:
    container, log, sink = watched
    incident = await detect(container)

    async def failing_save(_: Incident) -> None:
        raise RuntimeError("the database is down")

    container.incidents.repository.save = failing_save  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="database is down"):
        await container.incidents.raise_severity(incident, Severity.CRITICAL, reason="SLA breached", actor="workflow")
    assert NotificationKind.INCIDENT_ESCALATED not in [e.kind for e in sink.events]
    assert ("publish", NotificationKind.INCIDENT_ESCALATED.value) not in log


async def test_an_execution_is_announced_after_its_outcome_is_stored(
    watched: tuple[Container, Log, OrderedSink],
) -> None:
    container, log, sink = watched
    incident = await detect(container)
    investigated, _ = await container.investigations.investigate(incident.incident_id, actor=OPERATOR)
    approval_id = investigated.approval_ids[0]
    await container.approvals.decide(approval_id, principal=OPERATOR, approve=True)
    await container.gateway.execute(approval_id, principal=ADMIN)

    announced = log.index(("publish", NotificationKind.ACTION_EXECUTED.value))
    # The claim and the finished outcome are both stored before people are told that it ran.
    stored = [entry for entry in log[:announced] if entry[0] == "transition-approval"]
    assert [status for _, status in stored][-2:] == ["EXECUTING", "SUCCEEDED"]
    assert NotificationKind.ACTION_EXECUTED in [e.kind for e in sink.events]
