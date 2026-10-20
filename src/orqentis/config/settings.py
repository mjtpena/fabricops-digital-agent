"""Typed settings. Everything comes from the environment; no credentials live in code."""

from __future__ import annotations

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
    # Global budget for Fabric and Power BI calls, shared by every collector, tool and refresh (rest mode).
    fabric_max_concurrency: int = Field(default=4, ge=1, le=64)
    fabric_rate_per_second: float = Field(default=5.0, gt=0, le=1000)
    fabric_rate_burst: int = Field(default=10, ge=1, le=1000)
    # Safety cap on pages read per job-instance listing (Fabric does not document the result order).
    fabric_job_instances_max_pages: int = Field(default=10, ge=1, le=1000)

    # Incremental collection: since (watermark - overlap), never further back than the maximum lookback.
    collection_max_lookback_hours: int = Field(default=168, ge=1, le=24 * 90)
    collection_overlap_minutes: int = Field(default=15, ge=0, le=24 * 60)

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

    # Autonomous runtime. The scheduler defaults to on outside ``local`` and off in ``local`` (see _guard_rails).
    scheduler_enabled: bool = False
    scheduler_tick_seconds: float = Field(default=5.0, gt=0, le=60)
    scheduler_lease_ttl_seconds: float = Field(default=60.0, ge=5, le=3600)
    scheduler_job_timeout_minutes: int = Field(default=30, ge=1, le=24 * 60)
    scheduler_collect_enabled: bool = True
    scheduler_collect_interval_minutes: int = Field(default=5, ge=1, le=24 * 60)
    scheduler_auto_investigate_enabled: bool = True
    scheduler_auto_investigate_interval_minutes: int = Field(default=1, ge=1, le=24 * 60)
    scheduler_auto_investigate_batch_size: int = Field(default=5, ge=1, le=50)
    scheduler_auto_investigate_max_attempts: int = Field(default=3, ge=1, le=20)
    scheduler_sla_sweep_enabled: bool = True
    scheduler_sla_sweep_interval_minutes: int = Field(default=15, ge=1, le=24 * 60)
    scheduler_catalog_refresh_enabled: bool = True
    scheduler_catalog_refresh_interval_minutes: int = Field(default=60, ge=1, le=7 * 24 * 60)
    investigation_max_concurrency: int = Field(default=2, ge=1, le=16)
    investigation_timeout_seconds: int = Field(default=600, ge=30, le=6 * 3600)

    # Startup and readiness. The container is built in the background; /health is liveness only.
    startup_grace_seconds: float = Field(default=10.0, ge=0, le=60)
    build_retry_initial_seconds: float = Field(default=2.0, gt=0, le=600)
    build_retry_max_seconds: float = Field(default=60.0, gt=0, le=3600)

    # API
    api_auth_mode: ApiAuthMode = ApiAuthMode.DEV_HEADERS

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
        if "scheduler_enabled" not in self.model_fields_set:
            # Deployed environments run autonomously; a developer laptop only does so when asked.
            self.scheduler_enabled = self.deployment_environment != "local"
        if self.build_retry_max_seconds < self.build_retry_initial_seconds:
            raise ValueError("ORQENTIS_BUILD_RETRY_MAX_SECONDS must be >= ORQENTIS_BUILD_RETRY_INITIAL_SECONDS")
        if self.scheduler_tick_seconds * 2 > self.scheduler_lease_ttl_seconds:
            # The lease is renewed once less than half its TTL is left; a slower tick would let it lapse between ticks.
            raise ValueError(
                "ORQENTIS_SCHEDULER_LEASE_TTL_SECONDS must be at least twice ORQENTIS_SCHEDULER_TICK_SECONDS"
            )
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
