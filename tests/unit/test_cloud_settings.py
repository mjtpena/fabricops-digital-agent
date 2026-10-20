"""Sovereign clouds, token scopes, service-principal inputs and the time zone: configurable, and wired through."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

import azure.identity as identity
import azure.identity.aio as identity_aio
import pytest
from pydantic import ValidationError

from orqentis.config.settings import (
    PUBLIC_FABRIC_SCOPE,
    PUBLIC_POWERBI_SCOPE,
    AuthMode,
    Settings,
)
from orqentis.fabric.rest import auth
from orqentis.fabric.rest.auth import (
    FABRIC_SCOPE,
    POWERBI_SCOPE,
    AzureIdentityTokenProvider,
    ServicePrincipalConfigError,
    build_async_credential,
    build_sync_credential,
    build_token_provider,
    ensure_service_principal_configured,
    service_principal_problems,
)
from tests.support.fake_entra import FakeEntraTransport

US_GOV_AUTHORITY = "https://login.microsoftonline.us"
# A placeholder: the Fabric resource of a sovereign cloud is whatever Microsoft documents for that cloud.
US_GOV_FABRIC_SCOPE = "https://api.fabric.example.invalid/.default"
US_GOV_POWERBI_SCOPE = "https://high.analysis.usgovcloudapi.net/powerbi/api/.default"
CHINA_POWERBI_SCOPE = "https://analysis.chinacloudapi.cn/powerbi/api/.default"
TENANT = "00000000-0000-0000-0000-00000000fab1"


def settings(**values: object) -> Settings:
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def gov_settings(**values: object) -> Settings:
    return settings(
        authority_host=US_GOV_AUTHORITY,
        fabric_scope=US_GOV_FABRIC_SCOPE,
        powerbi_scope=US_GOV_POWERBI_SCOPE,
        **values,
    )


# ------------------------------------------------------------------------------------------ settings
def test_defaults_are_the_public_cloud() -> None:
    s = settings()
    assert s.authority_host is None  # azure-identity's own default (login.microsoftonline.com)
    assert (s.fabric_scope, s.powerbi_scope) == (PUBLIC_FABRIC_SCOPE, PUBLIC_POWERBI_SCOPE)
    assert (FABRIC_SCOPE, POWERBI_SCOPE) == (PUBLIC_FABRIC_SCOPE, PUBLIC_POWERBI_SCOPE)
    assert PUBLIC_FABRIC_SCOPE == "https://api.fabric.microsoft.com/.default"
    assert PUBLIC_POWERBI_SCOPE == "https://analysis.windows.net/powerbi/api/.default"
    assert s.llm_auth_mode is None
    assert s.default_timezone == "Australia/Sydney"


def test_cloud_settings_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORQENTIS_AUTHORITY_HOST", "login.chinacloudapi.cn")
    monkeypatch.setenv("ORQENTIS_FABRIC_SCOPE", "https://api.fabric.example.cn/.default")
    monkeypatch.setenv("ORQENTIS_POWERBI_SCOPE", CHINA_POWERBI_SCOPE)
    monkeypatch.setenv("ORQENTIS_LLM_AUTH_MODE", "managed_identity")
    monkeypatch.setenv("ORQENTIS_DEFAULT_TIMEZONE", "Asia/Shanghai")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.authority_host == "https://login.chinacloudapi.cn"  # a bare host gets https://
    assert s.fabric_scope == "https://api.fabric.example.cn/.default"
    assert s.powerbi_scope == CHINA_POWERBI_SCOPE
    assert s.llm_auth_mode is AuthMode.MANAGED_IDENTITY
    assert s.default_timezone == "Asia/Shanghai"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://login.microsoftonline.us", "https://login.microsoftonline.us"),
        ("https://login.microsoftonline.us/", "https://login.microsoftonline.us"),
        ("login.microsoftonline.us", "https://login.microsoftonline.us"),
        ("  https://login.chinacloudapi.cn  ", "https://login.chinacloudapi.cn"),
        ("", None),
    ],
)
def test_authority_host_is_normalised(raw: str, expected: str | None) -> None:
    assert settings(authority_host=raw).authority_host == expected


@pytest.mark.parametrize(
    "raw",
    [
        "http://login.microsoftonline.us",  # token requests are never sent in clear text
        "https://login.microsoftonline.us/tenant-id",  # a host, not a tenant-specific authority
        "https://login.microsoftonline.us?x=1",
        "ftp://login.microsoftonline.us",
    ],
)
def test_authority_host_must_be_an_https_host(raw: str) -> None:
    with pytest.raises(ValidationError, match="https host"):
        settings(authority_host=raw)


@pytest.mark.parametrize(
    "raw", ["", "https://api.fabric.microsoft.com", "api.fabric.microsoft.com/.default", "/.default"]
)
def test_scopes_must_be_resource_uris_ending_in_default(raw: str) -> None:
    with pytest.raises(ValidationError, match=r"/\.default"):
        settings(fabric_scope=raw)
    with pytest.raises(ValidationError, match=r"/\.default"):
        settings(powerbi_scope=raw)


def test_api_style_scopes_are_accepted() -> None:
    assert settings(powerbi_scope="api://11111111-2222-3333-4444-555555555555/.default").powerbi_scope.startswith(
        "api://"
    )


@pytest.mark.parametrize("timezone", ["Australia/Sydney", "UTC", "America/Argentina/Buenos_Aires", "Europe/London"])
def test_valid_time_zones_are_accepted(timezone: str) -> None:
    assert settings(default_timezone=timezone).default_timezone == timezone


@pytest.mark.parametrize("timezone", ["Mars/Olympus", "", "Sydney", "../../etc/passwd", "Australia"])
def test_invalid_time_zones_fail_at_startup(timezone: str) -> None:
    with pytest.raises(ValidationError, match="IANA time zone"):
        settings(default_timezone=timezone)


# -------------------------------------------------------------------- credentials: the authority host
class Recorder:
    """Stands in for an azure-identity credential class and records how it was built."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        return SimpleNamespace(name=self.name, kwargs=kwargs)


CREDENTIALS = (
    "DefaultAzureCredential",
    "ManagedIdentityCredential",
    "WorkloadIdentityCredential",
    "EnvironmentCredential",
)


@pytest.fixture
def recorders(monkeypatch: pytest.MonkeyPatch) -> dict[tuple[str, str], Recorder]:
    found: dict[tuple[str, str], Recorder] = {}
    for module_name, module in (("aio", identity_aio), ("sync", identity)):
        for name in CREDENTIALS:
            recorder = Recorder(name)
            monkeypatch.setattr(module, name, recorder)
            found[(module_name, name)] = recorder
    return found


MODES = [
    (AuthMode.DEFAULT, "DefaultAzureCredential", True),
    (AuthMode.MANAGED_IDENTITY, "ManagedIdentityCredential", False),  # the platform issues the token: no authority
    (AuthMode.WORKLOAD_IDENTITY, "WorkloadIdentityCredential", True),
    (AuthMode.SERVICE_PRINCIPAL, "EnvironmentCredential", True),
]


@pytest.mark.parametrize(("mode", "name", "takes_authority"), MODES)
def test_authority_host_reaches_every_credential_that_talks_to_entra(
    recorders: dict[tuple[str, str], Recorder], mode: AuthMode, name: str, takes_authority: bool
) -> None:
    s = gov_settings(auth_mode=mode)
    asynchronous, synchronous = build_async_credential(s), build_sync_credential(s)
    assert asynchronous.name == synchronous.name == name  # type: ignore[attr-defined]
    for kind in ("aio", "sync"):
        (call,) = recorders[(kind, name)].calls
        assert call.get("authority") == (US_GOV_AUTHORITY if takes_authority else None), (kind, name)


@pytest.mark.parametrize(("mode", "name", "takes_authority"), MODES)
def test_defaults_pass_no_authority_so_public_cloud_behaviour_is_unchanged(
    recorders: dict[tuple[str, str], Recorder], mode: AuthMode, name: str, takes_authority: bool
) -> None:
    s = settings(auth_mode=mode)
    build_async_credential(s)
    build_sync_credential(s)
    for kind in ("aio", "sync"):
        (call,) = recorders[(kind, name)].calls
        assert "authority" not in call, (kind, name)


def test_default_credential_keeps_the_managed_identity_client_id_and_adds_the_authority(
    recorders: dict[tuple[str, str], Recorder],
) -> None:
    build_async_credential(gov_settings(managed_identity_client_id="mi-client"))
    (call,) = recorders[("aio", "DefaultAzureCredential")].calls
    assert call["managed_identity_client_id"] == "mi-client" and call["authority"] == US_GOV_AUTHORITY


# -------------------------------------------------------------------- the LLM credential (cross-tenant)
def test_llm_credential_can_stay_a_managed_identity_while_fabric_uses_a_service_principal(
    recorders: dict[tuple[str, str], Recorder],
) -> None:
    s = settings(
        auth_mode=AuthMode.SERVICE_PRINCIPAL,
        llm_auth_mode=AuthMode.MANAGED_IDENTITY,
        managed_identity_client_id="reader-mi",
        authority_host=US_GOV_AUTHORITY,
    )
    assert build_async_credential(s).name == "EnvironmentCredential"  # type: ignore[attr-defined]  # Fabric
    assert build_sync_credential(s).name == "ManagedIdentityCredential"  # type: ignore[attr-defined]  # Azure OpenAI
    assert recorders[("sync", "ManagedIdentityCredential")].calls == [{"client_id": "reader-mi"}]
    assert recorders[("sync", "EnvironmentCredential")].calls == []  # the foreign-tenant secret never reaches the LLM


def test_llm_credential_follows_auth_mode_when_unset(recorders: dict[tuple[str, str], Recorder]) -> None:
    assert build_sync_credential(settings(auth_mode=AuthMode.WORKLOAD_IDENTITY)).name == "WorkloadIdentityCredential"  # type: ignore[attr-defined]


# ------------------------------------------------------------------------------------- the real wire
@pytest.fixture
def entra(monkeypatch: pytest.MonkeyPatch) -> FakeEntraTransport:
    """Real azure-identity credentials talking to a fake Entra ID."""
    transport = FakeEntraTransport()
    monkeypatch.setattr(auth, "_async_transport_options", lambda: {"transport": transport})
    monkeypatch.setenv("AZURE_TENANT_ID", TENANT)
    monkeypatch.setenv("AZURE_CLIENT_ID", "00000000-0000-0000-0000-0000000000c1")
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "test-only-not-a-real-secret")
    return transport


async def test_us_government_authority_and_scopes_reach_the_token_requests(entra: FakeEntraTransport) -> None:
    provider = build_token_provider(gov_settings(auth_mode=AuthMode.SERVICE_PRINCIPAL))
    assert await provider.get_token(FABRIC_SCOPE) == "entra-issued-token"
    assert await provider.get_token(POWERBI_SCOPE) == "entra-issued-token"
    await provider.close()
    requests = entra.token_requests()
    assert [url for url, _ in requests] == [f"{US_GOV_AUTHORITY}/{TENANT}/oauth2/v2.0/token"] * 2
    assert [form["scope"] for _, form in requests] == [US_GOV_FABRIC_SCOPE, US_GOV_POWERBI_SCOPE]
    assert {urlsplit(url).netloc for _, url, _ in entra.sent} == {
        "login.microsoftonline.us"
    }  # nothing went to the public cloud


async def test_china_scopes_replace_the_public_ones(entra: FakeEntraTransport) -> None:
    s = settings(
        auth_mode=AuthMode.SERVICE_PRINCIPAL,
        authority_host="https://login.chinacloudapi.cn",
        powerbi_scope=CHINA_POWERBI_SCOPE,
    )
    provider = build_token_provider(s)
    await provider.get_token(POWERBI_SCOPE)
    await provider.get_token(FABRIC_SCOPE)  # unchanged: Fabric's scope was not overridden
    await provider.close()
    (powerbi, fabric) = entra.token_requests()
    assert powerbi[0].startswith("https://login.chinacloudapi.cn/") and powerbi[1]["scope"] == CHINA_POWERBI_SCOPE
    assert fabric[1]["scope"] == PUBLIC_FABRIC_SCOPE


async def test_defaults_request_the_public_cloud_authority_and_scopes(entra: FakeEntraTransport) -> None:
    provider = build_token_provider(settings(auth_mode=AuthMode.SERVICE_PRINCIPAL))
    await provider.get_token(FABRIC_SCOPE)
    await provider.get_token(POWERBI_SCOPE)
    await provider.close()
    requests = entra.token_requests()
    assert [url for url, _ in requests] == [f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token"] * 2
    assert [form["scope"] for _, form in requests] == [PUBLIC_FABRIC_SCOPE, PUBLIC_POWERBI_SCOPE]


async def test_token_provider_requests_the_mapped_scope_from_the_credential() -> None:
    requested: list[str] = []

    class Credential:
        async def get_token(self, *scopes: str, **kwargs: Any) -> Any:
            requested.append(scopes[0])
            return SimpleNamespace(token="t", expires_on=10**12)

        async def close(self) -> None: ...

    provider = AzureIdentityTokenProvider(Credential(), scopes={FABRIC_SCOPE: US_GOV_FABRIC_SCOPE})  # type: ignore[arg-type]
    await provider.get_token(FABRIC_SCOPE)
    await provider.get_token(POWERBI_SCOPE)  # unmapped scopes pass through
    assert requested == [US_GOV_FABRIC_SCOPE, POWERBI_SCOPE]


# ---------------------------------------------------------------------- service principal inputs
def test_service_principal_problems_name_variables_never_values() -> None:
    assert service_principal_problems({}) == [
        "AZURE_TENANT_ID is not set",
        "AZURE_CLIENT_ID is not set",
        "neither AZURE_CLIENT_SECRET nor AZURE_CLIENT_CERTIFICATE_PATH is set",
    ]
    base = {"AZURE_TENANT_ID": TENANT, "AZURE_CLIENT_ID": "c"}
    assert service_principal_problems({**base, "AZURE_CLIENT_SECRET": "s"}) == []
    assert service_principal_problems({**base, "AZURE_CLIENT_CERTIFICATE_PATH": "/mnt/fabric-sp/sp.pem"}) == []
    both = service_principal_problems(
        {**base, "AZURE_CLIENT_SECRET": "very-secret", "AZURE_CLIENT_CERTIFICATE_PATH": "/p"}
    )
    assert both == ["set exactly one of AZURE_CLIENT_SECRET and AZURE_CLIENT_CERTIFICATE_PATH, not both"]


def test_service_principal_mode_fails_fast_when_it_is_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET", "AZURE_CLIENT_CERTIFICATE_PATH"):
        monkeypatch.delenv(name, raising=False)
    s = settings(auth_mode=AuthMode.SERVICE_PRINCIPAL)
    with pytest.raises(ServicePrincipalConfigError, match=r"AZURE_TENANT_ID is not set.*AZURE_CLIENT_ID is not set"):
        build_token_provider(s)
    ensure_service_principal_configured(settings(auth_mode=AuthMode.MANAGED_IDENTITY), {})  # other modes need nothing
    ensure_service_principal_configured(s, {"AZURE_TENANT_ID": "t", "AZURE_CLIENT_ID": "c", "AZURE_CLIENT_SECRET": "s"})


def test_bicep_sets_what_service_principal_mode_needs() -> None:
    """The template and the runtime agree on the variable names (they are the contract between them)."""
    template = Path(__file__).resolve().parents[2].joinpath("infra", "bicep", "main.bicep").read_text(encoding="utf-8")
    for name in ("AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET", "AZURE_CLIENT_CERTIFICATE_PATH"):
        assert f"'{name}'" in template, name
    for name in (
        "ORQENTIS_DEFAULT_TIMEZONE",
        "ORQENTIS_AUTHORITY_HOST",
        "ORQENTIS_FABRIC_SCOPE",
        "ORQENTIS_POWERBI_SCOPE",
        "ORQENTIS_LLM_AUTH_MODE",
    ):
        assert f"'{name}'" in template, name
