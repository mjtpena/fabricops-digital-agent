"""Notifications: tell people when an incident opens or escalates, an approval is requested or an action ran (ADR-0018).

The domain services publish :class:`~orqentis.notifications.model.NotificationEvent` values to a
:class:`~orqentis.notifications.sink.NotificationSink`. With no notifier configured that sink discards them. With one,
a :class:`~orqentis.notifications.dispatcher.NotificationDispatcher` redacts and bounds the content, de-duplicates by
event id, queues it in a bounded outbox and delivers it through a :class:`~orqentis.notifications.port.Notifier`
adapter (Teams Workflows webhook, generic webhook) in the background. Publishing never blocks and never raises, so a
failing webhook can not slow down or break detection, investigation or the approval boundary.
"""
