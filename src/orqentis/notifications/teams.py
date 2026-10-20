"""Microsoft Teams Workflows webhook adapter ("When a Teams webhook request is received", Power Automate).

Posts an Adaptive Card with **links only**: no ``Action.Submit``, ``Action.Execute`` or ``Action.OpenUrl``. Approving
in Teams would be a second approval surface that has to re-implement the human, role, fingerprint and expiry checks
(ADR-0010), so the card just tells people what happened and links to the UI where those checks run.

The body is the shape the Teams documentation gives for webhooks: a ``message`` with an Adaptive Card attachment. Every
string was cleaned by ``orqentis.notifications.content``; on top of that this adapter defuses the Markdown that Teams
renders in card text, so a Fabric item named ``[Re-enter your password](https://evil.example)`` shows as text and never
becomes a link in a card that people trust.
"""

from __future__ import annotations

import json
from typing import Any

from orqentis import __version__
from orqentis.notifications.model import NotificationKind, NotificationMessage
from orqentis.notifications.port import DeliveryError, DeliveryReceipt
from orqentis.notifications.ssrf import WebhookPoster

ADAPTIVE_CARD_CONTENT_TYPE = "application/vnd.microsoft.card.adaptive"
# Teams rejects a message above 28 KB. The content limits keep a card far below it; this is a hard stop.
MAX_BODY_BYTES = 24_000

_KIND_TITLES = {
    NotificationKind.INCIDENT_OPENED: "Incident opened",
    NotificationKind.INCIDENT_ESCALATED: "Incident escalated",
    NotificationKind.APPROVAL_REQUESTED: "Approval requested",
    NotificationKind.ACTION_EXECUTED: "Action executed",
    NotificationKind.ACTION_FAILED: "Action failed",
    NotificationKind.TEST: "Test notification",
}
_ACCENT = {
    NotificationKind.INCIDENT_OPENED: "Attention",
    NotificationKind.INCIDENT_ESCALATED: "Attention",
    NotificationKind.APPROVAL_REQUESTED: "Warning",
    NotificationKind.ACTION_EXECUTED: "Good",
    NotificationKind.ACTION_FAILED: "Attention",
    NotificationKind.TEST: "Accent",
}
# Brackets build links and images, angle brackets build autolinks. Swapped for look-alikes that do nothing.
_DEFUSE = str.maketrans({"[": "(", "]": ")", "<": chr(0x2039), ">": chr(0x203A)})  # look-alike angle quotes


def card_text(text: str) -> str:
    """Untrusted text for a card: readable, but unable to form a Markdown link, image or autolink."""
    return text.translate(_DEFUSE)


def _link_target(url: str) -> str:
    return url.replace("(", "%28").replace(")", "%29").replace(" ", "%20")


def build_card(message: NotificationMessage) -> dict[str, Any]:
    subtitle = " · ".join(
        part
        for part in (
            card_text(_KIND_TITLES[message.kind]),
            card_text(message.severity) if message.severity else None,
            card_text(message.environment) if message.environment else None,
            message.occurred_at.strftime("%Y-%m-%d %H:%M UTC"),
        )
        if part
    )
    body: list[dict[str, Any]] = [
        {
            "type": "TextBlock",
            "text": card_text(message.headline),
            "weight": "Bolder",
            "size": "Medium",
            "wrap": True,
            "color": _ACCENT[message.kind],
        },
        {"type": "TextBlock", "text": subtitle, "isSubtle": True, "spacing": "None", "wrap": True},
    ]
    if message.facts:
        body.append(
            {
                "type": "FactSet",
                "facts": [{"title": card_text(label), "value": card_text(value)} for label, value in message.facts],
            }
        )
    body.append(
        {
            "type": "TextBlock",
            # The one link in the card, built from the configured public base URL and an id, never from event text.
            "text": f"[{message.link_label}]({_link_target(message.link_url)})",
            "wrap": True,
        }
    )
    content: dict[str, Any] = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "msteams": {"width": "Full"},
        "body": body,
    }
    return {
        "type": "message",
        "attachments": [{"contentType": ADAPTIVE_CARD_CONTENT_TYPE, "contentUrl": None, "content": content}],
    }


class TeamsWorkflowsNotifier:
    name = "teams_workflows"

    def __init__(self, poster: WebhookPoster) -> None:
        self._poster = poster

    @property
    def destination(self) -> str:
        return self._poster.destination

    async def send(self, message: NotificationMessage) -> DeliveryReceipt:
        body = json.dumps(build_card(message), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(body) > MAX_BODY_BYTES:  # unreachable with the content limits; refuse rather than let Teams reject it
            raise DeliveryError("CARD_TOO_LARGE", "the card exceeds the Teams size limit", retryable=False)
        status = await self._poster.post(
            body, {"Content-Type": "application/json", "User-Agent": f"orqentis-fabricops/{__version__}"}
        )
        return DeliveryReceipt(status_code=status)
