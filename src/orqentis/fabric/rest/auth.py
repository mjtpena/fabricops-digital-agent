"""Microsoft Entra ID token acquisition for the Fabric and Power BI REST APIs.

No credential lives in code or settings: the credential *type* is selected by ``ORQENTIS_AUTH_MODE``
and azure-identity reads its inputs from the platform (managed identity endpoint, federated token
file, or ``AZURE_CLIENT_ID`` / ``AZURE_TENANT_ID`` / ``AZURE_CLIENT_SECRET`` | certificate variables).

Sovereign clouds: ``ORQENTIS_AUTHORITY_HOST`` is passed to every credential that talks to Entra ID, and
``ORQENTIS_FABRIC_SCOPE`` / ``ORQENTIS_POWERBI_SCOPE`` replace the public-cloud resources in token requests.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import time
from collections.abc import Callable, Mapping
from typing import Any, Protocol, assert_never

import azure.identity as identity
import azure.identity.aio as identity_aio
from azure.core.credentials import AccessToken, TokenCredential
from azure.core.credentials_async import AsyncTokenCredential
from azure.core.exceptions import ClientAuthenticationError

from orqentis.config.settings import PUBLIC_FABRIC_SCOPE, PUBLIC_POWERBI_SCOPE, AuthMode, Settings
from orqentis.fabric.errors import FabricAuthError, ServicePrincipalConfigError
from orqentis.observability.redaction import redact_text

# The adapter asks for these logical scopes. ORQENTIS_FABRIC_SCOPE / ORQENTIS_POWERBI_SCOPE redirect them to
# another cloud's resources when the token is requested (see AzureIdentityTokenProvider), so nothing above
# the token provider needs to know which cloud it runs in.
FABRIC_SCOPE = PUBLIC_FABRIC_SCOPE
POWERBI_SCOPE = PUBLIC_POWERBI_SCOPE

# Refresh tokens this long before they expire so a request never carries an about-to-expire token.
_REFRESH_MARGIN_SECONDS = 300.0


class TokenProvider(Protocol):
    async def get_token(self, scope: str) -> str:
        """Return a bearer access token for ``scope``."""
        ...


class AzureIdentityTokenProvider:
    """Caches azure-identity access tokens per scope until shortly before they expire.

    ``scopes`` maps the scope the adapter asks for to the scope actually requested from Entra ID, which is how
    a sovereign cloud's Fabric and Power BI resources replace the public-cloud ones.
    """

    def __init__(
        self,
        credential: AsyncTokenCredential,
        *,
        clock: Callable[[], float] = time.time,
        scopes: Mapping[str, str] | None = None,
    ) -> None:
        self._credential = credential
        self._clock = clock
        self._scopes = dict(scopes or {})
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
            requested = self._scopes.get(scope, scope)
            try:
                access = await self._credential.get_token(requested)
            except ClientAuthenticationError as exc:
                raise FabricAuthError(
                    f"Failed to acquire an Entra ID token for {requested}: {redact_text(str(exc))[:500]}",
                    error_code="TokenAcquisitionFailed",
                    details={"scope": requested, "credential_error": type(exc).__name__},
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


# Variables EnvironmentCredential reads in service_principal mode. The secret or certificate arrives from a
# Key Vault reference in Container Apps (infra/bicep); it is never in this repository.
_SP_TENANT, _SP_CLIENT = "AZURE_TENANT_ID", "AZURE_CLIENT_ID"
_SP_SECRET, _SP_CERTIFICATE = "AZURE_CLIENT_SECRET", "AZURE_CLIENT_CERTIFICATE_PATH"


def service_principal_problems(environ: Mapping[str, str]) -> list[str]:
    """What is missing or ambiguous for service_principal mode (variable names only, never values)."""
    problems = [f"{name} is not set" for name in (_SP_TENANT, _SP_CLIENT) if not environ.get(name)]
    has_secret, has_certificate = bool(environ.get(_SP_SECRET)), bool(environ.get(_SP_CERTIFICATE))
    if has_secret and has_certificate:
        problems.append(f"set exactly one of {_SP_SECRET} and {_SP_CERTIFICATE}, not both")
    elif not has_secret and not has_certificate:
        problems.append(f"neither {_SP_SECRET} nor {_SP_CERTIFICATE} is set")
    return problems


def ensure_service_principal_configured(settings: Settings, environ: Mapping[str, str] | None = None) -> None:
    """Fail fast, naming the missing variables, when service_principal mode lacks its inputs.

    Without this, azure-identity only fails at the first token request with a generic "authentication
    unavailable". Other auth modes need nothing here.
    """
    if settings.auth_mode is not AuthMode.SERVICE_PRINCIPAL:
        return
    if problems := service_principal_problems(os.environ if environ is None else environ):
        raise ServicePrincipalConfigError(
            "ORQENTIS_AUTH_MODE=service_principal is not configured: "
            + "; ".join(problems)
            + ". Provide them from a secret store (a Key Vault reference), never from the repository."
        )


def _authority(settings: Settings) -> dict[str, str]:
    """``authority`` for credentials that talk to Entra ID; empty keeps azure-identity's own default."""
    return {"authority": settings.authority_host} if settings.authority_host else {}


def build_async_credential(settings: Settings, mode: AuthMode | None = None) -> AsyncTokenCredential:
    """Async credential for the FabricOps operating identity, chosen by ``settings.auth_mode`` (or ``mode``)."""
    mode, client_id = mode or settings.auth_mode, settings.managed_identity_client_id
    options = {**_async_transport_options(), **_authority(settings)}
    if mode is AuthMode.DEFAULT:
        # Passing managed_identity_client_id=None would override AZURE_CLIENT_ID, so only pass it when set.
        if client_id:
            return identity_aio.DefaultAzureCredential(managed_identity_client_id=client_id, **options)
        return identity_aio.DefaultAzureCredential(**options)
    if mode is AuthMode.MANAGED_IDENTITY:
        # The managed identity endpoint has no authority host: the platform issues the token.
        return identity_aio.ManagedIdentityCredential(client_id=client_id, **_async_transport_options())
    if mode is AuthMode.WORKLOAD_IDENTITY:
        return identity_aio.WorkloadIdentityCredential(**options)
    if mode is AuthMode.SERVICE_PRINCIPAL:
        return identity_aio.EnvironmentCredential(**options)
    assert_never(mode)


def build_token_provider(settings: Settings) -> AzureIdentityTokenProvider:
    """Token provider for the Fabric and Power BI APIs, with the configured cloud's scopes."""
    ensure_service_principal_configured(settings)
    return AzureIdentityTokenProvider(
        build_async_credential(settings),
        scopes={FABRIC_SCOPE: settings.fabric_scope, POWERBI_SCOPE: settings.powerbi_scope},
    )


def build_sync_credential(settings: Settings) -> TokenCredential:
    """Sync counterpart of :func:`build_async_credential` (used by Agent Framework chat clients).

    It follows ``llm_auth_mode`` when that is set: the LLM may live in a different Entra tenant from the
    Fabric estate (managed identity for Azure OpenAI, a service principal for a foreign Fabric tenant).
    """
    mode, client_id = settings.llm_auth_mode or settings.auth_mode, settings.managed_identity_client_id
    authority = _authority(settings)
    if mode is AuthMode.DEFAULT:
        if client_id:
            return identity.DefaultAzureCredential(managed_identity_client_id=client_id, **authority)
        return identity.DefaultAzureCredential(**authority)
    if mode is AuthMode.MANAGED_IDENTITY:
        return identity.ManagedIdentityCredential(client_id=client_id)
    if mode is AuthMode.WORKLOAD_IDENTITY:
        return identity.WorkloadIdentityCredential(**authority)
    if mode is AuthMode.SERVICE_PRINCIPAL:
        return identity.EnvironmentCredential(**authority)
    assert_never(mode)


class InteractiveTokenProvider:
    """Tokens for a human who signs in in the browser. Used only by ``orqentis contracts scaffold --scanner``.

    The Power BI admin Scanner API needs a Fabric administrator, which none of the application's own identities
    may be. The application runtime never builds this provider: it is not one of the ``AuthMode`` values.
    """

    def __init__(self, settings: Settings, *, client_id: str | None = None, tenant_id: str | None = None) -> None:
        options: dict[str, Any] = dict(_authority(settings))
        if client_id:
            options["client_id"] = client_id
        if tenant := tenant_id or settings.tenant_id:
            options["tenant_id"] = tenant
        self._credential = identity.InteractiveBrowserCredential(**options)
        self._scopes = {FABRIC_SCOPE: settings.fabric_scope, POWERBI_SCOPE: settings.powerbi_scope}

    async def get_token(self, scope: str) -> str:
        requested = self._scopes.get(scope, scope)
        try:
            access = await asyncio.to_thread(self._credential.get_token, requested)
        except ClientAuthenticationError as exc:
            raise FabricAuthError(
                f"Interactive sign-in failed for {requested}: {redact_text(str(exc))[:500]}",
                error_code="TokenAcquisitionFailed",
                details={"scope": requested, "credential_error": type(exc).__name__},
            ) from exc
        return access.token

    async def close(self) -> None:
        self._credential.close()
