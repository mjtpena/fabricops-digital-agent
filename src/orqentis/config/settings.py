"""Typed settings. Everything comes from the environment; no credentials live in code."""

from __future__ import annotations

import ipaddress
from enum import StrEnum
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from orqentis.notifications.urlcheck import UnsafeDestinationError, check_webhook_url

# Token scopes of the public cloud. Sovereign clouds use other resources (see docs/development.md).
PUBLIC_FABRIC_SCOPE = "https://api.fabric.microsoft.com/.default"
PUBLIC_POWERBI_SCOPE = "https://analysis.windows.net/powerbi/api/.default"


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


class NotifierKind(StrEnum):
    """Where notifications go (ADR-0010, ADR-0018)."""

    NULL = "null"  # nowhere: local development, the offline demo and tests
    TEAMS_WORKFLOWS = "teams_workflows"  # Microsoft Teams Workflows webhook, an Adaptive Card with links only
    WEBHOOK = "webhook"  # generic JSON webhook, optionally signed with HMAC-SHA256


# Hosts a Power Automate / Workflows webhook URL can have (public and sovereign clouds). Used when the Teams notifier
# is selected and ORQENTIS_NOTIFY_ALLOWED_HOSTS is empty. ``*.`` matches any number of labels below the suffix.
DEFAULT_TEAMS_WEBHOOK_HOSTS: tuple[str, ...] = (
    "*.logic.azure.com",
    "*.logic.azure.us",
    "*.logic.azure.cn",
    "*.api.powerplatform.com",
    "*.api.gov.powerplatform.microsoft.us",
    "*.api.high.powerplatform.microsoft.us",
    "*.api.powerplatform.partner.microsoftonline.cn",
)


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
    # Sovereign clouds: the Entra ID authority host and the token scopes of the Fabric and Power BI APIs.
    # Unset authority_host leaves azure-identity's own default (public cloud, or AZURE_AUTHORITY_HOST).
    authority_host: str | None = None
    fabric_scope: str = PUBLIC_FABRIC_SCOPE
    powerbi_scope: str = PUBLIC_POWERBI_SCOPE
    # Credential type for non-Fabric Azure calls (the LLM). Unset follows auth_mode. Set managed_identity when
    # auth_mode=service_principal targets a Fabric tenant other than the one that hosts Azure OpenAI.
    llm_auth_mode: AuthMode | None = None
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
    # Wait this long after start before taking the lease or running a job, so the previous revision can drain during a
    # deploy (Container Apps runs both on the same volume). Unset: 120 outside ``local``, 0 in ``local`` (see below).
    scheduler_startup_delay_seconds: float = Field(default=0.0, ge=0, le=900)
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

    # Human interface (ADR-0010, ADR-0018): notifications and the server-rendered web UI under /ui.
    # The origin people reach the UI at (https://<fqdn>); deep links in notifications are built from it.
    public_base_url: str | None = None
    notifier: NotifierKind = NotifierKind.NULL
    # Webhook URLs are secrets (the Teams URL carries its signature in the query string). In Azure they are Container
    # App secrets that reference Key Vault. They are never logged, audited or echoed in an error.
    notify_teams_webhook_url: SecretStr | None = None
    notify_webhook_url: SecretStr | None = None
    notify_webhook_signing_secret: SecretStr | None = None  # generic webhook: HMAC-SHA256 key (optional)
    # Comma-separated host patterns a webhook URL may point to (exact name, or "*.suffix"). Empty: the Power Platform
    # hosts for the Teams notifier, no host restriction for the generic webhook (the address checks always apply).
    notify_allowed_hosts: str = ""
    notify_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    notify_max_attempts: int = Field(default=5, ge=1, le=10)
    notify_backoff_base_seconds: float = Field(default=2.0, gt=0, le=300)
    notify_backoff_max_seconds: float = Field(default=60.0, gt=0, le=3600)
    notify_outbox_size: int = Field(default=200, ge=1, le=10_000)
    ui_enabled: bool = True
    # Path of the Easy Auth sign-in endpoint (for example /.auth/login/aad). When set, deep links in notifications go
    # through it: a person without a session is sent to Entra ID and then on to the page.
    ui_sign_in_path: str | None = None
    # HMAC key of the CSRF tokens in the UI forms. Unset: a random key per process, enough for one replica. With several
    # replicas set it (a Key Vault secret), or a form rendered by one replica is refused by another.
    ui_csrf_secret: SecretStr | None = None

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

    @field_validator("default_timezone")
    @classmethod
    def _valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (KeyError, ValueError, OSError):  # unknown key, malformed key, or a directory name
            raise ValueError(f"{value!r} is not a valid IANA time zone (for example Australia/Sydney)") from None
        return value

    @field_validator("authority_host")
    @classmethod
    def _valid_authority_host(cls, value: str | None) -> str | None:
        """An https host such as ``https://login.microsoftonline.us`` (a bare host name gets ``https://``)."""
        if value is None or not value.strip():
            return None
        raw = value.strip()
        parts = urlsplit(raw if "://" in raw else f"https://{raw}")
        if parts.scheme != "https" or not parts.hostname or parts.path.strip("/") or parts.query or parts.fragment:
            raise ValueError("must be an https host such as https://login.microsoftonline.us (no path or query)")
        return f"https://{parts.netloc}"

    @field_validator("public_base_url")
    @classmethod
    def _valid_public_base_url(cls, value: str | None) -> str | None:
        """The origin of the UI: https (http only for a loopback host), no path, query, fragment or credentials."""
        if value is None or not value.strip():
            return None
        parts = urlsplit(value.strip())
        problem = (
            "must be an origin such as https://fabricops.example.com (no path, query or credentials)"
            if parts.path.strip("/") or parts.query or parts.fragment or parts.username or parts.password
            else None
        )
        host = parts.hostname
        if problem is None and (host is None or parts.scheme not in {"https", "http"}):
            problem = "must be an https origin such as https://fabricops.example.com"
        if problem is None and parts.scheme == "http" and not (host and _is_loopback(host)):
            problem = "must use https (http is accepted for localhost only)"
        if problem is not None:
            raise ValueError(problem)
        return f"{parts.scheme}://{parts.netloc.lower()}"

    @field_validator("ui_sign_in_path")
    @classmethod
    def _valid_sign_in_path(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        path = value.strip()
        if not path.startswith("/") or path.startswith("//") or "\\" in path or "?" in path or "#" in path:
            raise ValueError("must be an absolute path on this host such as /.auth/login/aad")
        return path

    @field_validator("fabric_scope", "powerbi_scope")
    @classmethod
    def _valid_scope(cls, value: str) -> str:
        scope = value.strip()
        resource, _, suffix = scope.rpartition("/")
        if suffix != ".default" or urlsplit(resource).scheme not in ("https", "api") or not urlsplit(resource).netloc:
            raise ValueError(
                "must be a resource URI ending in /.default, e.g. https://api.fabric.microsoft.com/.default"
            )
        return scope

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
        if "scheduler_startup_delay_seconds" not in self.model_fields_set and self.deployment_environment != "local":
            self.scheduler_startup_delay_seconds = 120.0
        if self.build_retry_max_seconds < self.build_retry_initial_seconds:
            raise ValueError("ORQENTIS_BUILD_RETRY_MAX_SECONDS must be >= ORQENTIS_BUILD_RETRY_INITIAL_SECONDS")
        if self.scheduler_tick_seconds * 3 > self.scheduler_lease_ttl_seconds:
            # Renewal is attempted once a third of the TTL is left, so with TTL >= 3 ticks there is always at least
            # one whole tick between a renewal attempt and expiry. A tick delayed by more than that can lose the lease.
            raise ValueError(
                "ORQENTIS_SCHEDULER_LEASE_TTL_SECONDS must be at least three times ORQENTIS_SCHEDULER_TICK_SECONDS"
            )
        self._check_notifications()
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

    def _check_notifications(self) -> None:
        """A notifier needs its secret URL and a public base URL; messages never contain the URL itself."""
        if self.notify_backoff_max_seconds < self.notify_backoff_base_seconds:
            raise ValueError("ORQENTIS_NOTIFY_BACKOFF_MAX_SECONDS must be >= ORQENTIS_NOTIFY_BACKOFF_BASE_SECONDS")
        if self.notifier is NotifierKind.NULL:
            return
        variable, secret = (
            ("ORQENTIS_NOTIFY_TEAMS_WEBHOOK_URL", self.notify_teams_webhook_url)
            if self.notifier is NotifierKind.TEAMS_WORKFLOWS
            else ("ORQENTIS_NOTIFY_WEBHOOK_URL", self.notify_webhook_url)
        )
        raw = secret.get_secret_value().strip() if secret is not None else ""
        if not raw:
            raise ValueError(f"ORQENTIS_NOTIFIER={self.notifier.value} requires {variable} (a secret, never a setting)")
        try:
            check_webhook_url(raw, allowed_hosts=self.notify_allowed_host_patterns)
        except UnsafeDestinationError as exc:
            # The reason never contains the URL: it is a secret.
            raise ValueError(f"{variable} is not acceptable: {exc.reason}") from None
        if self.public_base_url is None:
            raise ValueError(
                f"ORQENTIS_NOTIFIER={self.notifier.value} requires ORQENTIS_PUBLIC_BASE_URL (the deep links in "
                "notifications are built from it)"
            )

    @property
    def notify_allowed_host_patterns(self) -> tuple[str, ...]:
        """Host patterns a webhook URL may use: the configured list, else the Power Platform hosts for Teams."""
        configured = tuple(p.strip().lower() for p in self.notify_allowed_hosts.split(",") if p.strip())
        if configured:
            return configured
        return DEFAULT_TEAMS_WEBHOOK_HOSTS if self.notifier is NotifierKind.TEAMS_WORKFLOWS else ()

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
