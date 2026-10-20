"""Background delivery: de-duplication, a bounded outbox, retries with back-off and an audit record per attempt.

``publish`` is what the domain services call. It is synchronous, constant time and **never raises**: it cleans the
event, drops it if its id was seen, and appends it to a bounded outbox. A single worker task takes messages from the
outbox and calls the notifier. When the destination is down the worker retries with exponential back-off and jitter,
then gives up on that message and moves on; meanwhile new events keep filling the outbox, and when it is full the
**oldest** pending message is dropped (and audited). Detection, investigation and the approval boundary therefore never
wait for, or fail because of, a webhook.

Every delivery attempt and every dropped or abandoned message is written to the hash-chained audit trail with the
event id, the notifier, the destination host (never the URL), the attempt number and the outcome.

The outbox is in memory: messages not yet delivered when the process dies are lost (recorded as ``ABANDONED`` on a
clean shutdown). A durable outbox belongs with the durable storage (ADR-0018).
"""

from __future__ import annotations

import asyncio
import random
import time
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable

from orqentis.notifications.content import DeepLinks, compose
from orqentis.notifications.model import NotificationEvent, NotificationMessage
from orqentis.notifications.port import DeliveryError, Notifier
from orqentis.observability.audit import AuditTrail
from orqentis.observability.logging import get_logger
from orqentis.observability.metrics import DISABLED, Metrics

_log = get_logger(__name__)

_RANDOM = random.SystemRandom()


def _jitter() -> float:
    return float(_RANDOM.random())


AUDIT_ACTOR = "system:notifications"
AUDIT_ACTION = "notification.send"

# What ends a message, as the ``orqentis.notifications`` metric records it. RETRYING is an attempt, not an end.
_METRIC_OUTCOMES = {"SENT": "sent", "FAILED": "failed", "DROPPED": "dropped", "ABANDONED": "abandoned"}


def backoff_delay(
    attempt: int, *, base: float, cap: float, jitter: Callable[[], float], retry_after: float | None = None
) -> float:
    """Seconds to wait after the ``attempt``-th failure: exponential, capped, with "equal jitter".

    The delay is between half of the ceiling and the ceiling, so retries of many messages do not line up, and never
    below the destination's own ``Retry-After`` (itself capped).
    """
    ceiling = min(cap, base * 2.0 ** (attempt - 1))
    delay = ceiling / 2 + jitter() * ceiling / 2
    if retry_after is not None:
        delay = max(delay, min(retry_after, cap))
    return delay


class NotificationDispatcher:
    def __init__(
        self,
        notifier: Notifier,
        audit: AuditTrail,
        links: DeepLinks,
        *,
        outbox_size: int = 200,
        max_attempts: int = 5,
        backoff_base: float = 2.0,
        backoff_max: float = 60.0,
        dedupe_capacity: int = 10_000,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[], float] = _jitter,
        drain_seconds: float = 5.0,
        metrics: Metrics = DISABLED,
    ) -> None:
        self._notifier = notifier
        self._audit = audit
        self._links = links
        self._outbox_size = outbox_size
        self._max_attempts = max_attempts
        self._backoff_base = backoff_base
        self._backoff_max = backoff_max
        self._dedupe_capacity = dedupe_capacity
        self._sleep = sleep
        self._jitter = jitter
        self._drain_seconds = drain_seconds
        self._metrics = metrics
        self._outbox: deque[NotificationMessage] = deque()
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._unaudited_drops: deque[NotificationMessage] = deque(maxlen=1000)
        self._wake: asyncio.Event | None = None
        self._task: asyncio.Task[None] | None = None
        self._closing = False
        self._busy = False
        self._idle = asyncio.Event()
        self._idle.set()
        self.dropped = 0

    # ----------------------------------------------------------------------------------------------- public
    @property
    def pending(self) -> int:
        return len(self._outbox)

    async def wait_idle(self, within_seconds: float = 10.0) -> None:
        """Wait until the outbox is empty and nothing is being delivered (tests, orderly shutdown)."""
        async with asyncio.timeout(within_seconds):
            await self._idle.wait()

    def publish(self, event: NotificationEvent) -> None:
        try:
            if event.event_id in self._seen:
                _log.debug("duplicate notification skipped", extra={"event_id": event.event_id})
                return
            message = compose(event, self._links)
            self._seen[event.event_id] = None
            while len(self._seen) > self._dedupe_capacity:
                self._seen.popitem(last=False)
            if len(self._outbox) >= self._outbox_size:
                dropped = self._outbox.popleft()
                self._seen.pop(dropped.event_id, None)  # it was never sent, so the same event may be published again
                self._unaudited_drops.append(dropped)
                self.dropped += 1
                _log.warning(
                    "notification outbox full; the oldest pending notification was dropped",
                    extra={"event_id": dropped.event_id, "outbox_size": self._outbox_size},
                )
            self._outbox.append(message)
            self._idle.clear()
            if self._wake is not None:
                self._wake.set()
        except Exception as exc:  # publishing must never break the caller
            _log.error("notification could not be queued", extra={"error_type": type(exc).__name__})

    async def start(self) -> None:
        if self._task is not None:
            return
        self._closing = False
        self._wake = asyncio.Event()
        if self._outbox:
            self._wake.set()
        self._task = asyncio.create_task(self._run(), name="notification-dispatcher")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        self._closing = True  # no more back-off sleeps: each queued message gets one last attempt
        if self._wake is not None:
            self._wake.set()
        try:
            await asyncio.wait_for(task, self._drain_seconds)
        except TimeoutError:
            pass  # wait_for cancelled the task
        except asyncio.CancelledError:
            task.cancel()
            raise
        except Exception as exc:  # the worker catches everything; this is belt and braces
            _log.error("notification worker ended with an error", extra={"error_type": type(exc).__name__})
        await self._flush_unaudited_drops()
        while self._outbox:
            message = self._outbox.popleft()
            await self._record(message, 0, "ABANDONED", reason="the process is shutting down")
        self._idle.set()
        self._closing = False

    # ---------------------------------------------------------------------------------------------- worker
    async def _run(self) -> None:
        wake = self._wake
        assert wake is not None
        while True:
            await self._flush_unaudited_drops()
            if not self._outbox:
                self._idle.set()
                if self._closing:
                    return
                wake.clear()
                await wake.wait()
                continue
            message = self._outbox.popleft()
            self._busy = True
            try:
                await self._deliver(message)
            except asyncio.CancelledError:
                await self._record(message, 0, "ABANDONED", reason="cancelled while the process was stopping")
                raise
            finally:
                self._busy = False

    async def _deliver(self, message: NotificationMessage) -> None:
        for attempt in range(1, self._max_attempts + 1):
            started = time.monotonic()
            try:
                receipt = await self._notifier.send(message)
            except DeliveryError as exc:
                error = exc
            except Exception as exc:  # an adapter bug: never retry, never put the exception text in the audit trail
                error = DeliveryError("UNEXPECTED_ERROR", f"unexpected {type(exc).__name__}", retryable=False)
            else:
                await self._record(
                    message, attempt, "SENT", status_code=receipt.status_code, elapsed=time.monotonic() - started
                )
                return
            elapsed = time.monotonic() - started
            if not error.retryable or attempt >= self._max_attempts or self._closing:
                await self._record(message, attempt, "FAILED", error=error, elapsed=elapsed)
                return
            delay = backoff_delay(
                attempt,
                base=self._backoff_base,
                cap=self._backoff_max,
                jitter=self._jitter,
                retry_after=error.retry_after,
            )
            await self._record(message, attempt, "RETRYING", error=error, elapsed=elapsed, retry_in=delay)
            await self._sleep(delay)

    # ---------------------------------------------------------------------------------------------- audit
    async def _flush_unaudited_drops(self) -> None:
        while self._unaudited_drops:
            await self._record(self._unaudited_drops.popleft(), 0, "DROPPED", reason="the outbox was full")

    async def _record(
        self,
        message: NotificationMessage,
        attempt: int,
        outcome: str,
        *,
        status_code: int | None = None,
        error: DeliveryError | None = None,
        elapsed: float | None = None,
        retry_in: float | None = None,
        reason: str | None = None,
    ) -> None:
        if (ended := _METRIC_OUTCOMES.get(outcome)) is not None:
            # Before the audit write, so an audit failure cannot hide a failed delivery from the metric.
            self._metrics.notification(self._notifier.name, ended)
        details: dict[str, object] = {
            "event_id": message.event_id,
            "kind": message.kind.value,
            "notifier": self._notifier.name,
            "destination": self._notifier.destination,
            "attempt": attempt,
            "max_attempts": self._max_attempts,
        }
        if message.approval_id:
            details["approval_id"] = message.approval_id
        if status_code is not None:
            details["status_code"] = status_code
        if error is not None:
            details["error_code"] = error.code
            details["error"] = error.message
            if error.status_code is not None:
                details["status_code"] = error.status_code
        if elapsed is not None:
            details["elapsed_ms"] = round(elapsed * 1000)
        if retry_in is not None:
            details["retry_in_seconds"] = round(retry_in, 1)
        if reason is not None:
            details["reason"] = reason
        try:
            await self._audit.record(
                actor=AUDIT_ACTOR,
                action=AUDIT_ACTION,
                outcome=outcome,
                incident_id=message.incident_id,
                details=details,
            )
        except Exception as exc:  # an audit failure must not stop notifications (it is logged and visible there)
            _log.error("notification audit record failed", extra={"error_type": type(exc).__name__})
