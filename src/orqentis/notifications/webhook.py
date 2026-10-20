"""Generic webhook adapter: the notification as JSON, optionally signed with HMAC-SHA256.

A receiver that wants to trust the origin of a call verifies ``X-Orqentis-Signature`` (``sha256=<hex>``), the HMAC of
``"<X-Orqentis-Timestamp>.<body>"`` under the shared secret, and rejects timestamps that are too old. The secret is a
Key Vault reference in Azure, like the URL.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Callable

from orqentis import __version__
from orqentis.notifications.model import NotificationMessage
from orqentis.notifications.port import DeliveryReceipt
from orqentis.notifications.ssrf import WebhookPoster

SCHEMA = "orqentis.notification.v1"
SIGNATURE_HEADER = "X-Orqentis-Signature"
TIMESTAMP_HEADER = "X-Orqentis-Timestamp"
EVENT_ID_HEADER = "X-Orqentis-Event-Id"


def build_payload(message: NotificationMessage) -> dict[str, object]:
    return {
        "schema": SCHEMA,
        "event_id": message.event_id,
        "kind": message.kind.value,
        "occurred_at": message.occurred_at.isoformat(),
        "incident_id": message.incident_id,
        "approval_id": message.approval_id,
        "headline": message.headline,
        "severity": message.severity,
        "environment": message.environment,
        "facts": [{"label": label, "value": value} for label, value in message.facts],
        "link": {"label": message.link_label, "url": message.link_url},
    }


def sign(secret: bytes, timestamp: str, body: bytes) -> str:
    digest = hmac.new(secret, timestamp.encode("ascii") + b"." + body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


class GenericWebhookNotifier:
    name = "webhook"

    def __init__(
        self, poster: WebhookPoster, *, signing_secret: bytes | None = None, clock: Callable[[], float] = time.time
    ) -> None:
        self._poster = poster
        self._secret = signing_secret
        self._clock = clock

    @property
    def destination(self) -> str:
        return self._poster.destination

    async def send(self, message: NotificationMessage) -> DeliveryReceipt:
        body = json.dumps(build_payload(message), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "User-Agent": f"orqentis-fabricops/{__version__}",
            EVENT_ID_HEADER: message.event_id,
        }
        if self._secret:
            timestamp = str(int(self._clock()))
            headers[TIMESTAMP_HEADER] = timestamp
            headers[SIGNATURE_HEADER] = sign(self._secret, timestamp, body)
        return DeliveryReceipt(status_code=await self._poster.post(body, headers))
