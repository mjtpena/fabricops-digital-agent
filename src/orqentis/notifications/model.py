"""What is announced: events raised by the domain, and the clean message that is actually sent."""

from __future__ import annotations

from enum import StrEnum

from pydantic import AwareDatetime, BaseModel, ConfigDict


class NotificationKind(StrEnum):
    INCIDENT_OPENED = "INCIDENT_OPENED"
    INCIDENT_ESCALATED = "INCIDENT_ESCALATED"
    APPROVAL_REQUESTED = "APPROVAL_REQUESTED"
    ACTION_EXECUTED = "ACTION_EXECUTED"
    ACTION_FAILED = "ACTION_FAILED"
    TEST = "TEST"  # sent by `orqentis notify-test` only, never by the services


Facts = tuple[tuple[str, str], ...]


class NotificationEvent(BaseModel):
    """Something worth telling a person about. Raised by the domain services; the text is NOT yet safe to send.

    ``headline`` and ``facts`` can contain Fabric metadata (item names, error messages): untrusted. The dispatcher
    sanitises, redacts and length-limits them (``orqentis.notifications.content``) before a message is built.
    ``event_id`` identifies the business event: publishing it twice sends it once.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str
    kind: NotificationKind
    occurred_at: AwareDatetime
    incident_id: str
    approval_id: str | None = None
    headline: str
    severity: str | None = None
    environment: str | None = None
    facts: Facts = ()


class NotificationMessage(BaseModel):
    """The clean, bounded content handed to a notifier. Every string has been through the redaction."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str
    kind: NotificationKind
    occurred_at: AwareDatetime
    incident_id: str
    approval_id: str | None = None
    headline: str
    severity: str | None = None
    environment: str | None = None
    facts: Facts = ()
    link_url: str
    link_label: str
