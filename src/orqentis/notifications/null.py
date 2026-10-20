"""Null notifier: accepts every message and sends it nowhere. For local development, the offline demo and tests."""

from __future__ import annotations

from orqentis.notifications.model import NotificationMessage
from orqentis.notifications.port import DeliveryReceipt


class NullNotifier:
    name = "null"
    destination = "none"

    async def send(self, message: NotificationMessage) -> DeliveryReceipt:
        return DeliveryReceipt()
