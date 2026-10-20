"""Typed settings. Everything comes from the environment; no credentials live in code."""

from __future__ import annotations

import ipaddress
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class FabricMode(StrEnum):
    MOCK = "mock"
    REST = "rest"


class ReasonerKind(StrEnum):
    RULE_BASED = "rule_based"
    AGENT_FRAMEWORK = "agent_framework"


class LlmProvider(StrEnum):
    AZURE_OPENAI = "azure_openai"
    FOUNDRY = "foundry"


class AuthMode(StrEnum):
    """How the FabricOps operating identity authenticates to Microsoft Entra ID."""

    DEFAULT = "default"  # DefaultAzureCredential chain (developer login, env, MI, ...)
    MANAGED_IDENTITY = "managed_identity"
    WORKLOAD_IDENTITY = "workload_identity"
    SERVICE_PRINCIPAL = "service_principal"  # AZURE_CLIENT_ID / AZURE_TENANT_ID / AZURE_CLIENT_SECRET|CERT


class ApiAuthMode(StrEnum):
    DEV_HEADERS = "dev_headers"  # local development only
    EASY_AUTH = "easy_auth"  # Azure Container Apps / App Service built-in Entra authentication


class ClockMode(StrEnum):
    SYSTEM = "system"
    SCENARIO = "scenario"  # freeze time at the synthetic scenario's "now" (mock mode only)


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ORQENTIS_", env_file=".env", extra="ignore")

    deployment_environment: Literal["local", "dev", "test", "prod"] = "local"
    tenant_id: str = "tenant-contoso"
    default_timezone: str = "Australia/Sydney"

    data_dir: Path = Path("var")
    fixtures_dir: Path = Path("fixtures")
    runbooks_dir: Path = Path("runbooks")
    policy_path: Path | None = None
    contracts_path: Path | None = None
    scenario: str = "schema_drift"
    clock: ClockMode = ClockMode.SCENARIO

    # Fabric integration
    fabric_mode: FabricMode = FabricMode.MOCK
    fabric_api_base_url: str = "https://api.fabric.microsoft.com/v1"
    powerbi_api_base_url: str = "https://api.powerbi.com/v1.0/myorg"
    auth_mode: AuthMode = AuthMode.DEFAULT
    managed_identity_client_id: str | None = None
    http_timeout_seconds: float = 30.0
    http_max_retries: int = Field(default=4, ge=0, le=10)

    # Agent / reasoning
    reasoner: ReasonerKind = ReasonerKind.RULE_BASED
    llm_provider: LlmProvider = LlmProvider.AZURE_OPENAI
    azure_openai_endpoint: str | None = None
    azure_openai_deployment: str | None = None
    azure_openai_api_version: str | None = None
    foundry_project_endpoint: str | None = None
    foundry_model: str | None = None
    agent_max_tool_calls: int = Field(default=12, ge=0, le=50)

    # Action / approval boundary
    enable_write_actions: bool = False
    auto_request_approval: bool = True
    approval_ttl_minutes: int = Field(default=240, ge=5)

    # API
    api_auth_mode: ApiAuthMode = ApiAuthMode.DEV_HEADERS
    # dev_headers lets every caller pick its own identity and roles, so `orqentis serve` refuses it on a
    # non-loopback address unless this is set (for example `docker run` of a local test container).
    dev_headers_allow_remote: bool = False
    # Roles for reads. None (default): required under easy_auth, not enforced under dev_headers (local only).
    # With enforcement every API call needs a FabricOps role (Reader, Automation, Operator or PlatformAdmin).
    # Collection and investigation always need their roles under easy_auth, whatever this says.
    api_require_read_role: bool | None = None
    # Request limits (per replica, in memory). Bodies over the limit get 413. Mutating calls and the expensive
    # ones (telemetry collection, investigations, audit verification) are limited per principal and get 429.
    api_max_body_bytes: int = Field(default=65_536, ge=1_024, le=16_777_216)
    api_rate_limit_enabled: bool = True
    api_rate_limit_mutating_per_minute: int = Field(default=30, ge=1, le=10_000)
    api_rate_limit_expensive_per_minute: int = Field(default=6, ge=1, le=10_000)

    # Observability
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "json"
    otel_console_exporter: bool = False
    applicationinsights_connection_string: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "ORQENTIS_APPLICATIONINSIGHTS_CONNECTION_STRING", "APPLICATIONINSIGHTS_CONNECTION_STRING"
        ),
    )

    @model_validator(mode="after")
    def _guard_rails(self) -> Settings:
        if self.api_auth_mode is ApiAuthMode.DEV_HEADERS and self.deployment_environment != "local":
            raise ValueError("ORQENTIS_API_AUTH_MODE=dev_headers is only permitted when deployment_environment=local")
        if self.fabric_mode is FabricMode.REST and "tenant_id" not in self.model_fields_set:
            raise ValueError("ORQENTIS_TENANT_ID is required in rest mode (it is stamped on every record)")
        if self.fabric_mode is FabricMode.REST and self.clock is ClockMode.SCENARIO:
            # A frozen clock only makes sense against synthetic telemetry.
            self.clock = ClockMode.SYSTEM
        if self.reasoner is ReasonerKind.AGENT_FRAMEWORK:
            if self.llm_provider is LlmProvider.AZURE_OPENAI and not (
                self.azure_openai_endpoint and self.azure_openai_deployment
            ):
                raise ValueError(
                    "reasoner=agent_framework with azure_openai requires "
                    "ORQENTIS_AZURE_OPENAI_ENDPOINT and ORQENTIS_AZURE_OPENAI_DEPLOYMENT"
                )
            if self.llm_provider is LlmProvider.FOUNDRY and not (self.foundry_project_endpoint and self.foundry_model):
                raise ValueError(
                    "reasoner=agent_framework with foundry requires "
                    "ORQENTIS_FOUNDRY_PROJECT_ENDPOINT and ORQENTIS_FOUNDRY_MODEL"
                )
        return self

    def check_bind_host(self, host: str) -> None:
        """Refuse to serve ``dev_headers`` (self-asserted identities and roles) on a non-loopback address."""
        if (
            self.api_auth_mode is ApiAuthMode.DEV_HEADERS
            and not self.dev_headers_allow_remote
            and not _is_loopback(host)
        ):
            raise ValueError(
                f"Refusing to serve ORQENTIS_API_AUTH_MODE=dev_headers on {host!r}: any caller could choose its own "
                "roles. Bind to 127.0.0.1, use ORQENTIS_API_AUTH_MODE=easy_auth, or set "
                "ORQENTIS_DEV_HEADERS_ALLOW_REMOTE=true for a throwaway local test container."
            )

    @property
    def read_role_required(self) -> bool:
        """Whether read endpoints need a FabricOps role (default: yes under easy_auth, no under dev_headers)."""
        if self.api_require_read_role is not None:
            return self.api_require_read_role
        return self.api_auth_mode is ApiAuthMode.EASY_AUTH

    @property
    def action_roles_required(self) -> bool:
        """Whether collection and investigation need their roles. Under easy_auth this cannot be turned off.

        ``api_require_read_role=false`` only opens the read endpoints. Locally (dev_headers) the roles apply
        when ``api_require_read_role=true`` asks for the production role model.
        """
        return self.api_auth_mode is ApiAuthMode.EASY_AUTH or self.api_require_read_role is True

    @property
    def event_store_path(self) -> Path:
        return self.data_dir / "events.sqlite3"

    @property
    def state_db_path(self) -> Path:
        return self.data_dir / "state.sqlite3"

    @property
    def evidence_dir(self) -> Path:
        return self.data_dir / "evidence"

    @property
    def audit_log_path(self) -> Path:
        return self.data_dir / "audit" / "audit.jsonl"

    @property
    def log_file_path(self) -> Path:
        return self.data_dir / "logs" / "orqentis.jsonl"
