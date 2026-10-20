"""The seam the domain services use to announce events. Cheap, synchronous, and never raises."""

from __future__ import annotations

from typing import Protocol

from orqentis.notifications.model import NotificationEvent


class NotificationSink(Protocol):
    def publish(self, event: NotificationEvent) -> None:
        """Hand an event over for delivery. Must return at once and must not raise."""
        ...


class NotificationService(NotificationSink, Protocol):
    """A sink with a life cycle: the API's runtime starts it with the scheduler and stops it on shutdown."""

    async def start(self) -> None: ...

    async def stop(self) -> None: ...


class NullNotificationService:
    """No notifier is configured: events are dropped on the floor (local development, the offline demo, tests)."""

    def publish(self, event: NotificationEvent) -> None:
        return None

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None


# Stateless, so one instance is the default for every service that takes a sink.
NOOP_SINK = NullNotificationService()
