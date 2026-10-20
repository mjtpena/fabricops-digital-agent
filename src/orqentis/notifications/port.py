"""The notifier port: one adapter per destination kind."""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, ConfigDict

from orqentis.notifications.model import NotificationMessage


class DeliveryError(Exception):
    """A delivery attempt failed. ``code`` is stable and safe to log and audit; the message never holds a URL.

    ``retryable`` says whether another attempt can succeed (a timeout, a 5xx, a 429) or not (a refused destination, a
    redirect, a 4xx that is not 408/429). ``retry_after`` is the destination's own back-off hint in seconds.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.status_code = status_code
        self.retry_after = retry_after


class DeliveryReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status_code: int | None = None


class Notifier(Protocol):
    """Delivers one message to one destination.

    ``name`` identifies the adapter and ``destination`` the target in a form that is safe to log and audit (a host
    name, never the URL: webhook URLs are secrets). ``send`` raises :class:`DeliveryError` when the attempt failed.
    """

    @property
    def name(self) -> str: ...

    @property
    def destination(self) -> str: ...

    async def send(self, message: NotificationMessage) -> DeliveryReceipt: ...
