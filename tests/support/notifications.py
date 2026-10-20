"""Fakes for the notification tests: a notifier that records (and can be told to fail or hang) and a recording sink."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable

from orqentis.notifications.model import NotificationEvent, NotificationMessage
from orqentis.notifications.port import DeliveryError, DeliveryReceipt

Failure = DeliveryError | Exception | None


class RecordingNotifier:
    """Implements the ``Notifier`` port. ``outcomes`` are consumed one per attempt: ``None`` means success."""

    name = "recording"
    destination = "hooks.example.test"

    def __init__(self, outcomes: Iterable[Failure] = (), *, hang: bool = False, default: Failure = None) -> None:
        self._outcomes = list(outcomes)
        self._default = default
        self._hang = hang
        self.attempts = 0
        self.sent: list[NotificationMessage] = []

    async def send(self, message: NotificationMessage) -> DeliveryReceipt:
        self.attempts += 1
        if self._hang:
            await asyncio.sleep(3600)
        outcome = self._outcomes.pop(0) if self._outcomes else self._default
        if outcome is not None:
            raise outcome
        self.sent.append(message)
        return DeliveryReceipt(status_code=202)


class RecordingSink:
    """Implements ``NotificationService`` and keeps the events it is given."""

    def __init__(self) -> None:
        self.events: list[NotificationEvent] = []

    def publish(self, event: NotificationEvent) -> None:
        self.events.append(event)

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None


def retryable(code: str = "HTTP_503", **kwargs: float | int | None) -> DeliveryError:
    return DeliveryError(code, f"the webhook answered {code}", retryable=True, **kwargs)  # type: ignore[arg-type]


def permanent(code: str = "HTTP_400") -> DeliveryError:
    return DeliveryError(code, f"the webhook answered {code}", retryable=False, status_code=400)
