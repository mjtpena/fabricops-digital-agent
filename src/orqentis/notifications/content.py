"""From an event to a message that is safe to send.

Everything that reaches a card or a webhook body passes through here: control characters and envelope tags are
removed, secrets are redacted (the same redaction as logs, evidence and audit), whitespace is collapsed and every field
is length-limited. Fabric metadata is untrusted (invariant 8), and a notification leaves the system for a chat
service or a third-party endpoint, so nothing is trusted just because the domain produced it.
"""

from __future__ import annotations

import re
from urllib.parse import quote

from orqentis.notifications.model import Facts, NotificationEvent, NotificationKind, NotificationMessage
from orqentis.observability.redaction import redact_text
from orqentis.tools.untrusted import sanitize_text
from orqentis.ui import paths

MAX_HEADLINE = 200
MAX_LABEL = 40
MAX_VALUE = 300
MAX_FACTS = 12
_WHITESPACE = re.compile(r"\s+")

_LINK_LABELS = {
    NotificationKind.INCIDENT_OPENED: "Open the incident",
    NotificationKind.INCIDENT_ESCALATED: "Open the incident",
    NotificationKind.APPROVAL_REQUESTED: "Review the approval request",
    NotificationKind.ACTION_EXECUTED: "Open the approval",
    NotificationKind.ACTION_FAILED: "Open the approval",
    NotificationKind.TEST: "Open FabricOps",
}


def clean(text: str, limit: int) -> str:
    """One line of untrusted text: no control characters or tags, secrets redacted, at most ``limit`` characters."""
    single_line = _WHITESPACE.sub(" ", redact_text(sanitize_text(str(text), max_length=4 * limit))).strip()
    return single_line if len(single_line) <= limit else single_line[: limit - 1].rstrip() + "…"


class DeepLinks:
    """Absolute links into the UI, built from the configured public base URL.

    With ``sign_in_path`` (the Easy Auth login endpoint) a link first passes through the sign-in, so a person without a
    session is sent to Entra ID and lands on the page afterwards. Without a base URL the links stay relative.
    """

    def __init__(self, base_url: str | None, *, sign_in_path: str | None = None) -> None:
        self._base = (base_url or "").rstrip("/")
        self._sign_in = sign_in_path

    def to(self, path: str) -> str:
        if self._sign_in:
            return f"{self._base}{self._sign_in}?post_login_redirect_uri={quote(path, safe='')}"
        return f"{self._base}{path}"

    def for_event(self, event: NotificationEvent) -> str:
        if event.kind is NotificationKind.TEST:
            return self.to(paths.incidents_path())
        if event.approval_id:
            return self.to(paths.approval_path(event.approval_id))
        return self.to(paths.incident_path(event.incident_id))


def _facts(facts: Facts) -> Facts:
    return tuple(
        (clean(label, MAX_LABEL), clean(value, MAX_VALUE)) for label, value in facts[:MAX_FACTS] if label and value
    )


def compose(event: NotificationEvent, links: DeepLinks) -> NotificationMessage:
    return NotificationMessage(
        event_id=event.event_id,
        kind=event.kind,
        occurred_at=event.occurred_at,
        incident_id=clean(event.incident_id, MAX_LABEL),
        approval_id=clean(event.approval_id, MAX_LABEL) if event.approval_id else None,
        headline=clean(event.headline, MAX_HEADLINE),
        severity=clean(event.severity, MAX_LABEL) if event.severity else None,
        environment=clean(event.environment, MAX_LABEL) if event.environment else None,
        facts=_facts(event.facts),
        link_url=links.for_event(event),
        link_label=_LINK_LABELS[event.kind],
    )
