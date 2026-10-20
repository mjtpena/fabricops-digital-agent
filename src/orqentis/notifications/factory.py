"""Builds the notification service from the settings. Called by the composition root only."""

from __future__ import annotations

import httpx

from orqentis.config.settings import NotifierKind, Settings
from orqentis.notifications.content import DeepLinks
from orqentis.notifications.dispatcher import NotificationDispatcher
from orqentis.notifications.port import Notifier
from orqentis.notifications.sink import NotificationService, NullNotificationService
from orqentis.notifications.ssrf import Resolver, WebhookPoster, system_resolver
from orqentis.notifications.teams import TeamsWorkflowsNotifier
from orqentis.notifications.webhook import GenericWebhookNotifier
from orqentis.observability.audit import AuditTrail


def build_notifier(
    settings: Settings,
    *,
    resolver: Resolver = system_resolver,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Notifier | None:
    """The adapter for ``settings.notifier``; ``None`` for the null notifier (nothing to deliver)."""
    if settings.notifier is NotifierKind.NULL:
        return None
    allowed = settings.notify_allowed_host_patterns
    if settings.notifier is NotifierKind.TEAMS_WORKFLOWS:
        assert settings.notify_teams_webhook_url is not None  # Settings refuses the combination otherwise
        poster = WebhookPoster(
            settings.notify_teams_webhook_url.get_secret_value().strip(),
            allowed_hosts=allowed,
            timeout=settings.notify_timeout_seconds,
            resolver=resolver,
            transport=transport,
        )
        return TeamsWorkflowsNotifier(poster)
    assert settings.notify_webhook_url is not None
    poster = WebhookPoster(
        settings.notify_webhook_url.get_secret_value().strip(),
        allowed_hosts=allowed,
        timeout=settings.notify_timeout_seconds,
        resolver=resolver,
        transport=transport,
    )
    secret = settings.notify_webhook_signing_secret
    return GenericWebhookNotifier(poster, signing_secret=secret.get_secret_value().encode() if secret else None)


def build_notification_service(
    settings: Settings,
    audit: AuditTrail,
    *,
    notifier: Notifier | None = None,
) -> NotificationService:
    """A dispatcher in front of the configured notifier, or a service that discards events when there is none.

    ``notifier`` replaces the configured adapter (tests, embedders).
    """
    chosen = notifier if notifier is not None else build_notifier(settings)
    if chosen is None:
        return NullNotificationService()
    return NotificationDispatcher(
        chosen,
        audit,
        DeepLinks(settings.public_base_url, sign_in_path=settings.ui_sign_in_path),
        outbox_size=settings.notify_outbox_size,
        max_attempts=settings.notify_max_attempts,
        backoff_base=settings.notify_backoff_base_seconds,
        backoff_max=settings.notify_backoff_max_seconds,
    )
