"""Delivery: retries with back-off and jitter, the limit, de-duplication, the bounded outbox and the audit trail."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from orqentis.config.clock import FixedClock
from orqentis.notifications.content import DeepLinks
from orqentis.notifications.dispatcher import AUDIT_ACTION, NotificationDispatcher, backoff_delay
from orqentis.notifications.model import NotificationEvent, NotificationKind
from orqentis.notifications.null import NullNotifier
from orqentis.observability.audit import AuditRecord, LocalAuditLog
from tests.support.notifications import RecordingNotifier, permanent, retryable

NOW = datetime(2026, 10, 20, 3, 0, tzinfo=UTC)
LINKS = DeepLinks("https://fabricops.example.com")


def event(event_id: str = "incident-opened:INC-2026-0001", headline: str = "Incident opened") -> NotificationEvent:
    return NotificationEvent(
        event_id=event_id,
        kind=NotificationKind.INCIDENT_OPENED,
        occurred_at=NOW,
        incident_id="INC-2026-0001",
        headline=headline,
        severity="HIGH",
        environment="PROD",
        facts=(("Item", "pl-ingest"),),
    )


class Sleeps:
    """Replaces ``asyncio.sleep`` in the dispatcher: records the delays and returns at once."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        await asyncio.sleep(0)


@pytest.fixture
def audit(tmp_path: Path) -> LocalAuditLog:
    return LocalAuditLog(tmp_path / "audit" / "audit.jsonl", FixedClock(NOW))


@pytest.fixture
def sleeps() -> Sleeps:
    return Sleeps()


def build(
    notifier: RecordingNotifier, audit: LocalAuditLog, sleeps: Sleeps, **kwargs: float | int
) -> NotificationDispatcher:
    options: dict[str, float | int] = {
        "max_attempts": 4,
        "backoff_base": 2.0,
        "backoff_max": 30.0,
        "drain_seconds": 0.05,  # a hanging notifier must not hold up the end of a test
    }
    options.update(kwargs)
    return NotificationDispatcher(notifier, audit, LINKS, sleep=sleeps, jitter=lambda: 0.5, **options)


async def sends(audit: LocalAuditLog) -> list[AuditRecord]:
    return [r for r in await audit.list() if r.action == AUDIT_ACTION]


@pytest.fixture
async def started() -> AsyncIterator[list[NotificationDispatcher]]:
    """Dispatchers a test starts; they are stopped afterwards whatever the test did."""
    running: list[NotificationDispatcher] = []
    yield running
    for dispatcher in running:
        await dispatcher.stop()


# ------------------------------------------------------------------------------------------------ back-off


def test_backoff_grows_exponentially_is_capped_and_never_below_half_the_ceiling() -> None:
    def delays(jitter: float) -> list[float]:
        return [backoff_delay(n, base=2.0, cap=20.0, jitter=lambda: jitter) for n in range(1, 7)]

    assert delays(1.0) == [2.0, 4.0, 8.0, 16.0, 20.0, 20.0]  # exponential, then capped
    assert delays(0.0) == [1.0, 2.0, 4.0, 8.0, 10.0, 10.0]  # "equal jitter": half of the ceiling at least


def test_backoff_never_goes_below_the_retry_after_hint_but_the_hint_is_capped() -> None:
    assert backoff_delay(1, base=2.0, cap=30.0, jitter=lambda: 0.0, retry_after=12.0) == 12.0
    assert backoff_delay(1, base=2.0, cap=30.0, jitter=lambda: 0.0, retry_after=3600.0) == 30.0
    assert backoff_delay(3, base=2.0, cap=30.0, jitter=lambda: 1.0, retry_after=1.0) == 8.0


# --------------------------------------------------------------------------------------- retries and limits


async def test_retries_with_backoff_and_stops_at_the_limit(
    audit: LocalAuditLog, sleeps: Sleeps, started: list[NotificationDispatcher]
) -> None:
    notifier = RecordingNotifier(default=retryable())  # always 503
    dispatcher = build(notifier, audit, sleeps, max_attempts=4)
    started.append(dispatcher)
    await dispatcher.start()
    dispatcher.publish(event())
    await dispatcher.wait_idle()

    assert notifier.attempts == 4 and notifier.sent == []  # exactly the limit, then it gave up
    assert sleeps.delays == [1.5, 3.0, 6.0]  # base 2 doubling, jitter 0.5: between half the ceiling and the ceiling
    assert [r.outcome for r in await sends(audit)] == ["RETRYING", "RETRYING", "RETRYING", "FAILED"]
    final = (await sends(audit))[-1]
    assert final.details["attempt"] == 4 and final.details["error_code"] == "HTTP_503"
    assert dispatcher.pending == 0


async def test_a_transient_failure_is_retried_and_then_delivered(
    audit: LocalAuditLog, sleeps: Sleeps, started: list[NotificationDispatcher]
) -> None:
    notifier = RecordingNotifier([retryable(), retryable(), None])
    dispatcher = build(notifier, audit, sleeps)
    started.append(dispatcher)
    await dispatcher.start()
    dispatcher.publish(event())
    await dispatcher.wait_idle()

    assert notifier.attempts == 3 and len(notifier.sent) == 1
    assert [r.outcome for r in await sends(audit)] == ["RETRYING", "RETRYING", "SENT"]
    assert len(sleeps.delays) == 2


async def test_a_permanent_failure_is_not_retried(
    audit: LocalAuditLog, sleeps: Sleeps, started: list[NotificationDispatcher]
) -> None:
    notifier = RecordingNotifier([permanent("DESTINATION_NOT_PUBLIC")])
    dispatcher = build(notifier, audit, sleeps)
    started.append(dispatcher)
    await dispatcher.start()
    dispatcher.publish(event())
    await dispatcher.wait_idle()

    assert notifier.attempts == 1 and sleeps.delays == []
    assert [r.outcome for r in await sends(audit)] == ["FAILED"]


async def test_the_destinations_retry_after_is_honoured(
    audit: LocalAuditLog, sleeps: Sleeps, started: list[NotificationDispatcher]
) -> None:
    notifier = RecordingNotifier([retryable("HTTP_429", status_code=429, retry_after=20.0), None])
    dispatcher = build(notifier, audit, sleeps)
    started.append(dispatcher)
    await dispatcher.start()
    dispatcher.publish(event())
    await dispatcher.wait_idle()
    assert sleeps.delays == [20.0]


async def test_an_adapter_bug_is_contained_and_not_retried(
    audit: LocalAuditLog, sleeps: Sleeps, started: list[NotificationDispatcher]
) -> None:
    notifier = RecordingNotifier([RuntimeError("secret=hunter2-in-an-exception")])
    dispatcher = build(notifier, audit, sleeps)
    started.append(dispatcher)
    await dispatcher.start()
    dispatcher.publish(event("incident-opened:one"))
    dispatcher.publish(event("incident-opened:two"))
    await dispatcher.wait_idle()

    records = await sends(audit)
    assert [r.outcome for r in records] == ["FAILED", "SENT"]  # the worker survived and delivered the next one
    assert records[0].details["error_code"] == "UNEXPECTED_ERROR"
    assert "hunter2" not in records[0].model_dump_json()  # the exception text never reaches the audit trail


# ------------------------------------------------------------------------------------ de-duplication


async def test_an_event_id_is_delivered_once(
    audit: LocalAuditLog, sleeps: Sleeps, started: list[NotificationDispatcher]
) -> None:
    notifier = RecordingNotifier()
    dispatcher = build(notifier, audit, sleeps)
    started.append(dispatcher)
    await dispatcher.start()
    for _ in range(3):
        dispatcher.publish(event("approval-requested:APR-1"))
    dispatcher.publish(event("approval-requested:APR-2"))
    await dispatcher.wait_idle()
    dispatcher.publish(event("approval-requested:APR-1"))  # also after it was delivered
    await dispatcher.wait_idle()

    assert [m.event_id for m in notifier.sent] == ["approval-requested:APR-1", "approval-requested:APR-2"]
    assert len([r for r in await sends(audit) if r.outcome == "SENT"]) == 2


# ----------------------------------------------------------------------------------------------- audit


async def test_every_attempt_is_audited_with_the_destination_host_and_no_url(
    audit: LocalAuditLog, sleeps: Sleeps, started: list[NotificationDispatcher]
) -> None:
    notifier = RecordingNotifier([retryable("TIMEOUT"), None])
    dispatcher = build(notifier, audit, sleeps)
    started.append(dispatcher)
    await dispatcher.start()
    dispatcher.publish(event("incident-opened:INC-2026-0001"))
    await dispatcher.wait_idle()

    first, second = await sends(audit)
    assert (first.outcome, second.outcome) == ("RETRYING", "SENT")
    for record, attempt in ((first, 1), (second, 2)):
        assert record.actor == "system:notifications" and record.incident_id == "INC-2026-0001"
        assert record.details["event_id"] == "incident-opened:INC-2026-0001"
        assert record.details["notifier"] == "recording" and record.details["destination"] == "hooks.example.test"
        assert record.details["attempt"] == attempt and record.details["max_attempts"] == 4
        assert "elapsed_ms" in record.details
    assert second.details["status_code"] == 202
    assert first.details["retry_in_seconds"] == 1.5 and first.details["error_code"] == "TIMEOUT"
    assert (await audit.verify()).valid


# ---------------------------------------------------------------------------------- bounded and non-blocking


async def test_the_outbox_is_bounded_and_the_oldest_is_dropped_and_audited(
    audit: LocalAuditLog, sleeps: Sleeps, started: list[NotificationDispatcher]
) -> None:
    notifier = RecordingNotifier()
    dispatcher = build(notifier, audit, sleeps, outbox_size=3)
    started.append(dispatcher)
    # Not started yet: nothing drains the outbox.
    for number in range(1, 6):
        dispatcher.publish(event(f"incident-opened:{number}"))
    assert dispatcher.pending == 3 and dispatcher.dropped == 2

    await dispatcher.start()
    await dispatcher.wait_idle()
    assert [m.event_id for m in notifier.sent] == [f"incident-opened:{n}" for n in (3, 4, 5)]
    dropped = [r for r in await sends(audit) if r.outcome == "DROPPED"]
    assert [r.details["event_id"] for r in dropped] == ["incident-opened:1", "incident-opened:2"]

    # A dropped event was never sent, so publishing it again is accepted.
    dispatcher.publish(event("incident-opened:1"))
    await dispatcher.wait_idle()
    assert notifier.sent[-1].event_id == "incident-opened:1"


async def test_a_hanging_webhook_never_blocks_the_publisher(
    audit: LocalAuditLog, sleeps: Sleeps, started: list[NotificationDispatcher]
) -> None:
    notifier = RecordingNotifier(hang=True)
    dispatcher = build(notifier, audit, sleeps, outbox_size=10)
    started.append(dispatcher)
    await dispatcher.start()
    loop = asyncio.get_running_loop()
    began = loop.time()
    for number in range(500):
        dispatcher.publish(event(f"incident-opened:{number}"))
        await asyncio.sleep(0)  # the worker is stuck inside the notifier; the publisher must not notice
    assert loop.time() - began < 2.0
    assert dispatcher.pending <= 10 and dispatcher.dropped >= 480


async def test_publish_never_raises(audit: LocalAuditLog, sleeps: Sleeps) -> None:
    dispatcher = build(RecordingNotifier(), audit, sleeps)
    # An event that cannot be composed (it was built without validation and has no fields).
    broken = NotificationEvent.model_construct(event_id="x", kind=NotificationKind.INCIDENT_OPENED)
    dispatcher.publish(broken)  # must not raise
    assert dispatcher.pending == 0


async def test_stop_gives_each_pending_message_one_last_attempt_and_audits_what_is_left(
    audit: LocalAuditLog, sleeps: Sleeps
) -> None:
    notifier = RecordingNotifier(hang=True)
    dispatcher = build(notifier, audit, sleeps)
    await dispatcher.start()
    for number in range(3):
        dispatcher.publish(event(f"incident-opened:{number}"))
    await asyncio.sleep(0.01)
    await dispatcher.stop()

    outcomes = [r.outcome for r in await sends(audit)]
    assert outcomes.count("ABANDONED") == 3  # one was in flight, two still waiting: all three are accounted for
    assert dispatcher.pending == 0
    await dispatcher.start()  # restartable
    await dispatcher.stop()


async def test_the_null_notifier_accepts_everything(audit: LocalAuditLog, sleeps: Sleeps) -> None:
    dispatcher = NotificationDispatcher(NullNotifier(), audit, LINKS, sleep=sleeps)
    await dispatcher.start()
    dispatcher.publish(event())
    await dispatcher.wait_idle()
    await dispatcher.stop()
    assert [r.outcome for r in await sends(audit)] == ["SENT"]
