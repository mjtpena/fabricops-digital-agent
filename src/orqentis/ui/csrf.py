"""CSRF tokens for the UI forms (ledger R6).

A browser session (Easy Auth cookie) is classified HUMAN, so a page on another site could make a signed-in browser POST
``/ui/approvals/{id}/approve``. Two independent checks stop that (ADR-0018): the cross-origin guard
(``orqentis.ui.security``) refuses requests whose ``Origin``/``Referer``/``Sec-Fetch-Site`` is not this application,
and every state-changing UI form carries a token that only a page served to *this* principal contains.

The token is stateless: ``v1.<expiry>.<nonce>.<HMAC-SHA256(key, principal id, expiry, nonce)>``. It is bound to the
authenticated principal (the Entra object id) and expires, so a token fetched by one person is useless for another and
a leaked old one dies. The random nonce makes every rendering different, which also blunts BREACH-style compression
attacks. The key comes from ``ORQENTIS_UI_CSRF_SECRET``; without it a random per-process key is used, which suits one
replica (a form rendered before a restart is refused afterwards and the page has to be reloaded).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from collections.abc import Callable

_VERSION = "v1"
DEFAULT_TTL_SECONDS = 4 * 60 * 60  # as long as an approval request stays open


class CsrfTokens:
    def __init__(
        self,
        key: bytes | None = None,
        *,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._key = key or secrets.token_bytes(32)
        self._ttl = ttl_seconds
        self._clock = clock

    def _mac(self, principal_id: str, expiry: int, nonce: str) -> str:
        message = json.dumps([_VERSION, principal_id, expiry, nonce], separators=(",", ":")).encode()
        digest = hmac.new(self._key, message, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).decode().rstrip("=")

    def issue(self, principal_id: str) -> str:
        expiry = int(self._clock()) + self._ttl
        nonce = secrets.token_urlsafe(9)
        return f"{_VERSION}.{expiry}.{nonce}.{self._mac(principal_id, expiry, nonce)}"

    def verify(self, token: str | None, principal_id: str) -> bool:
        if not token or len(token) > 256 or not token.isascii():  # compare_digest only takes ASCII text
            return False
        parts = token.split(".")
        if len(parts) != 4 or parts[0] != _VERSION or not parts[1].isdigit():
            return False
        expiry = int(parts[1])
        if expiry < self._clock():
            return False
        return hmac.compare_digest(self._mac(principal_id, expiry, parts[2]), parts[3])
