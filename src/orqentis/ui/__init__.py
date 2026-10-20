"""The server-rendered web UI under ``/ui`` (ADR-0018).

Kept free of imports on purpose: ``orqentis.ui.paths`` is also used by the notifications, which must not pull in the
web framework.
"""
