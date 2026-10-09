"""Microsoft Entra ID token acquisition for the Fabric and Power BI REST APIs.

No credential lives in code or settings: the credential *type* is selected by ``ORQENTIS_AUTH_MODE``
and azure-identity reads its inputs from the platform (managed identity endpoint, federated token
file, or ``AZURE_CLIENT_ID`` / ``AZURE_TENANT_ID`` / ``AZURE_CLIENT_SECRET`` | certificate variables).
"""

from __future__ import annotations

import asyncio
import importlib.util
import time
from collections.abc import Callable
from typing import Any, Protocol, assert_never

import azure.identity as identity
import azure.identity.aio as identity_aio
from azure.core.credentials import AccessToken, TokenCredential
from azure.core.credentials_async import AsyncTokenCredential
from azure.core.exceptions import ClientAuthenticationError

from orqentis.config.settings import AuthMode, Settings
from orqentis.fabric.errors import FabricAuthError
from orqentis.observability.redaction import redact_text

FABRIC_SCOPE = "https://api.fabric.microsoft.com/.default"
POWERBI_SCOPE = "https://analysis.windows.net/powerbi/api/.default"

# Refresh tokens this long before they expire so a request never carries an about-to-expire token.
_REFRESH_MARGIN_SECONDS = 300.0


class TokenProvider(Protocol):
    async def get_token(self, scope: str) -> str:
        """Return a bearer access token for ``scope``."""
        ...


class AzureIdentityTokenProvider:
    """Caches azure-identity access tokens per scope until shortly before they expire."""

    def __init__(self, credential: AsyncTokenCredential, *, clock: Callable[[], float] = time.time) -> None:
        self._credential = credential
        self._clock = clock
        self._cache: dict[str, AccessToken] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _fresh(self, scope: str) -> str | None:
        cached = self._cache.get(scope)
        if cached is not None and cached.expires_on - _REFRESH_MARGIN_SECONDS > self._clock():
            return cached.token
        return None

    async def get_token(self, scope: str) -> str:
        if (token := self._fresh(scope)) is not None:
            return token
        # One acquisition per scope at a time; concurrent callers reuse the result.
        async with self._locks.setdefault(scope, asyncio.Lock()):
            if (token := self._fresh(scope)) is not None:
                return token
            try:
                access = await self._credential.get_token(scope)
            except ClientAuthenticationError as exc:
                raise FabricAuthError(
                    f"Failed to acquire an Entra ID token for {scope}: {redact_text(str(exc))[:500]}",
                    error_code="TokenAcquisitionFailed",
                    details={"scope": scope, "credential_error": type(exc).__name__},
                ) from exc
            self._cache[scope] = access
            return access.token

    async def close(self) -> None:
        self._cache.clear()
        await self._credential.close()


def _async_transport_options() -> dict[str, Any]:
    """azure-identity's async credentials default to an aiohttp transport. When aiohttp is not installed,
    fall back to azure-core's requests-based async transport (requests is an azure-core dependency)."""
    if importlib.util.find_spec("aiohttp") is not None:
        return {}
    from azure.core.pipeline.transport import AsyncioRequestsTransport

    return {"transport": AsyncioRequestsTransport()}


def build_async_credential(settings: Settings) -> AsyncTokenCredential:
    """Async credential for the FabricOps operating identity, chosen by ``settings.auth_mode``."""
    mode, client_id = settings.auth_mode, settings.managed_identity_client_id
    options = _async_transport_options()
    if mode is AuthMode.DEFAULT:
        # Passing managed_identity_client_id=None would override AZURE_CLIENT_ID, so only pass it when set.
        if client_id:
            return identity_aio.DefaultAzureCredential(managed_identity_client_id=client_id, **options)
        return identity_aio.DefaultAzureCredential(**options)
    if mode is AuthMode.MANAGED_IDENTITY:
        return identity_aio.ManagedIdentityCredential(client_id=client_id, **options)
    if mode is AuthMode.WORKLOAD_IDENTITY:
        return identity_aio.WorkloadIdentityCredential(**options)
    if mode is AuthMode.SERVICE_PRINCIPAL:
        return identity_aio.EnvironmentCredential(**options)
    assert_never(mode)


def build_sync_credential(settings: Settings) -> TokenCredential:
    """Sync counterpart of :func:`build_async_credential` (used by Agent Framework chat clients)."""
    mode, client_id = settings.auth_mode, settings.managed_identity_client_id
    if mode is AuthMode.DEFAULT:
        if client_id:
            return identity.DefaultAzureCredential(managed_identity_client_id=client_id)
        return identity.DefaultAzureCredential()
    if mode is AuthMode.MANAGED_IDENTITY:
        return identity.ManagedIdentityCredential(client_id=client_id)
    if mode is AuthMode.WORKLOAD_IDENTITY:
        return identity.WorkloadIdentityCredential()
    if mode is AuthMode.SERVICE_PRINCIPAL:
        return identity.EnvironmentCredential()
    assert_never(mode)
