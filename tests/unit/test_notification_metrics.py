"""The notification dispatcher counts how each message ended: sent, failed, dropped or abandoned."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from orqentis.config.clock import FixedClock
from orqentis.notifications.content import DeepLinks
from orqentis.notifications.dispatcher import NotificationDispatcher
from orqentis.notifications.model import NotificationEvent, NotificationKind
from orqentis.observability.audit import LocalAuditLog
from tests.support.metrics import MetricsHarness
from tests.support.notifications import RecordingNotifier, permanent, retryable

NOW = datetime(2026, 10, 20, 3, 0, tzinfo=UTC)


def event(event_id: str) -> NotificationEvent:
    return NotificationEvent(
        event_id=event_id,
        kind=NotificationKind.INCIDENT_OPENED,
        occurred_at=NOW,
        incident_id="INC-2026-0001",
        headline="Incident opened",
        severity="HIGH",
        environment="PROD",
        facts=(("Item", "pl-ingest"),),
    )


async def no_sleep(_: float) -> None:
    await asyncio.sleep(0)


@pytest.fixture
def harness() -> MetricsHarness:
    return MetricsHarness()


@pytest.fixture
async def audit(tmp_path: Path) -> LocalAuditLog:
    return LocalAuditLog(tmp_path / "audit" / "audit.jsonl", FixedClock(NOW))


@pytest.fixture
async def started() -> AsyncIterator[list[NotificationDispatcher]]:
    running: list[NotificationDispatcher] = []
    yield running
    for dispatcher in running:
        await dispatcher.stop()


def build(
    notifier: RecordingNotifier, audit: LocalAuditLog, harness: MetricsHarness, **kwargs: int | float
) -> NotificationDispatcher:
    options: dict[str, int | float] = {"max_attempts": 3, "drain_seconds": 0.05}
    options.update(kwargs)
    return NotificationDispatcher(
        notifier,
        audit,
        DeepLinks("https://fabricops.example.com"),
        sleep=no_sleep,
        jitter=lambda: 0.5,
        metrics=harness.metrics,
        **options,
    )


async def test_a_delivered_message_is_sent_and_its_retries_are_not_outcomes(
    audit: LocalAuditLog, harness: MetricsHarness, started: list[NotificationDispatcher]
) -> None:
    notifier = RecordingNotifier([retryable(), retryable()])  # two failed attempts, then success
    dispatcher = build(notifier, audit, harness)
    started.append(dispatcher)
    await dispatcher.start()
    dispatcher.publish(event("opened:1"))
    await dispatcher.wait_idle()

    assert notifier.attempts == 3
    assert harness.total("orqentis.notifications", channel="recording", outcome="sent") == 1
    assert harness.total("orqentis.notifications", outcome="failed") == 0
    assert harness.total("orqentis.notifications") == 1  # the two RETRYING attempts are not counted


async def test_a_refused_message_and_one_whose_attempts_ran_out_are_failed(
    audit: LocalAuditLog, harness: MetricsHarness, started: list[NotificationDispatcher]
) -> None:
    refused = build(RecordingNotifier(default=permanent()), audit, harness)
    exhausted = build(RecordingNotifier(default=retryable()), audit, harness)
    started.extend([refused, exhausted])
    for dispatcher in (refused, exhausted):
        await dispatcher.start()
        dispatcher.publish(event(f"opened:{id(dispatcher)}"))
        await dispatcher.wait_idle()

    assert harness.total("orqentis.notifications", channel="recording", outcome="failed") == 2
    assert harness.total("orqentis.notifications", outcome="sent") == 0


async def test_a_message_pushed_out_of_a_full_outbox_is_dropped(
    audit: LocalAuditLog, harness: MetricsHarness, started: list[NotificationDispatcher]
) -> None:
    notifier = RecordingNotifier()
    dispatcher = build(notifier, audit, harness, outbox_size=1)
    started.append(dispatcher)
    dispatcher.publish(event("opened:1"))
    dispatcher.publish(event("opened:2"))  # the worker is not running yet: the first message is pushed out
    await dispatcher.start()
    await dispatcher.wait_idle()

    assert harness.total("orqentis.notifications", outcome="dropped") == 1
    assert harness.total("orqentis.notifications", outcome="sent") == 1


async def test_a_message_still_queued_when_the_process_stops_is_abandoned(
    audit: LocalAuditLog, harness: MetricsHarness
) -> None:
    dispatcher = build(RecordingNotifier(hang=True), audit, harness)
    await dispatcher.start()
    dispatcher.publish(event("opened:1"))
    dispatcher.publish(event("opened:2"))
    await asyncio.sleep(0)  # the worker takes the first message and hangs on it
    await dispatcher.stop()

    assert harness.total("orqentis.notifications", outcome="abandoned") >= 1
    assert harness.total("orqentis.notifications", outcome="sent") == 0


async def test_a_failing_audit_trail_does_not_hide_a_failed_delivery_from_the_metric(
    harness: MetricsHarness, started: list[NotificationDispatcher]
) -> None:
    class BrokenAudit:
        async def record(self, **_: object) -> None:
            raise OSError("the audit volume is unavailable")

    dispatcher = NotificationDispatcher(
        RecordingNotifier(default=permanent()),
        BrokenAudit(),  # type: ignore[arg-type]
        DeepLinks("https://fabricops.example.com"),
        sleep=no_sleep,
        metrics=harness.metrics,
        drain_seconds=0.05,
    )
    started.append(dispatcher)
    await dispatcher.start()
    dispatcher.publish(event("opened:1"))
    await dispatcher.wait_idle()

    assert harness.total("orqentis.notifications", channel="recording", outcome="failed") == 1
