"""An azure-core transport that plays Microsoft Entra ID, so real azure-identity credentials run offline.

It records every request a credential sends to its authority host and answers token requests, which proves
where a token request goes and which scope it asks for without any network or credentials.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit

from azure.core.pipeline.transport import AsyncHttpResponse, AsyncHttpTransport


class _Response(AsyncHttpResponse):  # type: ignore[misc]
    def __init__(self, request: Any, body: dict[str, Any]) -> None:
        super().__init__(request, None)
        self.status_code = 200
        self.reason = "OK"
        self.headers = {"Content-Type": "application/json"}
        self.content_type = "application/json"
        self._body = json.dumps(body).encode()

    def body(self) -> bytes:
        return self._body

    def text(self, encoding: str | None = None) -> str:
        return self._body.decode()

    async def load_body(self) -> None:
        return None


class FakeEntraTransport(AsyncHttpTransport):  # type: ignore[misc]
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, Any]] = []  # (method, url, form body)

    async def open(self) -> None: ...

    async def close(self) -> None: ...

    async def __aenter__(self) -> FakeEntraTransport:
        return self

    async def __aexit__(self, *exc_details: Any) -> None: ...

    async def send(self, request: Any, **kwargs: Any) -> Any:
        self.sent.append((request.method, request.url, request.body))
        authority = f"https://{urlsplit(request.url).netloc}"
        if "discovery/instance" in request.url:
            return _Response(
                request, {"tenant_discovery_endpoint": f"{authority}/t/v2.0/.well-known/openid-configuration"}
            )
        if "openid-configuration" in request.url:
            return _Response(
                request,
                {
                    "authorization_endpoint": f"{authority}/t/oauth2/v2.0/authorize",
                    "token_endpoint": f"{authority}/t/oauth2/v2.0/token",
                    "issuer": f"{authority}/t/v2.0",
                },
            )
        return _Response(request, {"access_token": "entra-issued-token", "expires_in": 3600, "token_type": "Bearer"})

    def token_requests(self) -> list[tuple[str, dict[str, str]]]:
        """``(url, form)`` of every request to a ``/token`` endpoint."""
        return [(url, dict(body)) for method, url, body in self.sent if method == "POST" and url.endswith("/token")]
