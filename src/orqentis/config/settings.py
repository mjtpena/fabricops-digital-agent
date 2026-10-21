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
# Azure Storage (OneLake speaks the ADLS Gen2 API) and Kusto/Eventhouse (workspace monitoring). Public cloud values.
PUBLIC_STORAGE_SCOPE = "https://storage.azure.com/.default"
PUBLIC_KUSTO_SCOPE = "https://kusto.kusto.windows.net/.default"
PUBLIC_ONELAKE_BASE_URL = "https://onelake.dfs.fabric.microsoft.com"
# Azure Database for PostgreSQL (public cloud). Sovereign clouds use another resource (ORQENTIS_POSTGRES_SCOPE).
PUBLIC_POSTGRES_SCOPE = "https://ossrdbms-aad.database.windows.net/.default"


class FabricMode(StrEnum):
    MOCK = "mock"
    REST = "rest"


class RefreshSource(StrEnum):
    """Where semantic model refresh outcomes come from (decision D9, ADR-0020)."""

    WORKSPACE_MONITORING = "workspace_monitoring"  # the monitoring Eventhouse of each workspace: needs workspace Viewer
    POWERBI_API = "powerbi_api"  # the Power BI refresh history: needs dataset Write, which the reader never gets
    BOTH = "both"
    NONE = "none"


# The collectors that exist, for validating ORQENTIS_EXPECTED_FORBIDDEN_COLLECTORS (a typo must not pass silently).
COLLECTOR_NAMES: tuple[str, ...] = (
    "pipeline-runs",
    "notebook-runs",
    "spark-job-runs",
    "dataflow-runs",
    "semantic-model-refreshes",
    "workspace-monitoring-refreshes",
    "delta-schema-changes",
    "capacity-metrics",
)


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


class StorageBackend(StrEnum):
    LOCAL = "local"  # SQLite, filesystem evidence and a JSONL audit log: development, tests, mock mode, `make demo`
    POSTGRES = "postgres"  # PostgreSQL system of record + Blob evidence and audit anchors (deployed; ADR-0008, 0017)


class PostgresAuth(StrEnum):
    ENTRA = "entra"  # Microsoft Entra access token as the password (managed identity in Azure). The only deployed mode
    NONE = "none"  # no password at all, for a throwaway local container with trust authentication


class PostgresSslMode(StrEnum):
    VERIFY_FULL = "verify-full"  # TLS, certificate chain and host name verified (default)
    REQUIRE = "require"  # TLS, certificate not verified
    DISABLE = "disable"  # no TLS, for a throwaway local container only


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
    # hide_input_in_errors: a validation error must never print the raw settings it was given. Without it pydantic shows
    # ``input_value`` (a truncated copy of every variable read), so a start-up failure would put webhook URLs, the
    # signing key and the CSRF key into a traceback and from there into the logs.
    model_config = SettingsConfigDict(
        env_prefix="ORQENTIS_", env_file=".env", extra="ignore", hide_input_in_errors=True
    )

    deployment_environment: Literal["local", "dev", "test", "prod"] = "local"
    tenant_id: str = "tenant-contoso"
    default_timezone: str = "Australia/Sydney"

    data_dir: Path = Path("var")
    fixtures_dir: Path = Path("fixtures")
    runbooks_dir: Path = Path("runbooks")
    policy_path: Path | None = None
    # The estate contracts file. Local/mock runs read ``contracts_path``; with the postgres storage backend the file is
    # a blob (``contracts_container``/``contracts_blob_name`` in the evidence account) that a person uploads. An
    # explicit ``contracts_path`` wins over the blob (a developer machine pointing at a file).
    contracts_path: Path | None = None
    contracts_container: str = Field(default="contracts", pattern=r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")
    contracts_blob_name: str = "estate.json"
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

    # Signal sources beyond pipelines (ADR-0020). Each reads with the reader identity's proven minimum grants only
    # (docs/security.md section 11); none can write.
    # Job instances of notebooks, Spark job definitions and Dataflows (workspace Viewer).
    collect_item_jobs: bool = True
    # The _delta_log of the tables declared in the contracts (a OneLake data access role with Read on those tables).
    collect_schema_changes: bool = True
    delta_log_max_commits: int = Field(default=50, ge=1, le=1000)
    delta_log_max_commit_bytes: int = Field(default=2 * 1024 * 1024, ge=1024, le=64 * 1024 * 1024)
    onelake_base_url: str = PUBLIC_ONELAKE_BASE_URL
    onelake_scope: str = PUBLIC_STORAGE_SCOPE
    # Semantic model refresh outcomes. Workspace monitoring is the default in rest mode because the reader never gets
    # dataset Write; mock mode defaults to powerbi_api (the synthetic scenarios replay a Power BI shaped history).
    refresh_source: RefreshSource = RefreshSource.WORKSPACE_MONITORING
    workspace_monitoring_database: str = Field(default="Monitoring KQL database", min_length=1, max_length=256)
    kusto_scope: str = PUBLIC_KUSTO_SCOPE
    # The Fabric Capacity Metrics app (rest mode): the workspace it is installed in and its semantic model.
    # Both or neither.
    capacity_metrics_workspace_id: str | None = Field(default=None, pattern=r"^[0-9a-fA-F-]{36}$")
    capacity_metrics_dataset_id: str | None = Field(default=None, pattern=r"^[0-9a-fA-F-]{36}$")
    # Comma-separated collector names for which an HTTP 403 is expected and documented (for example
    # ``semantic-model-refreshes`` when ORQENTIS_REFRESH_SOURCE includes powerbi_api). Reported as "capability
    # unavailable", not as a collector error. Any other 401/403 stays an error.
    expected_forbidden_collectors: str = ""
    # A breach is declared only if the collector observing the freshness item completed a pass this recently
    # (0: no check).
    # Unset: 30 in rest mode, 0 in mock mode (synthetic telemetry has no collector watermarks to be stale).
    sla_max_telemetry_lag_minutes: int = Field(default=0, ge=0, le=24 * 60)

    # Storage (ADR-0008, ADR-0017). ``local``: SQLite files, filesystem evidence and a JSONL audit log under data_dir.
    # ``postgres``: the PostgreSQL system of record and Blob evidence, safe at two or more replicas. Deployed
    # environments (dev, test, prod) must use ``postgres``. There is no password setting: the database accepts an
    # Entra access token only.
    storage_backend: StorageBackend = StorageBackend.LOCAL
    postgres_host: str | None = None
    postgres_port: int = Field(default=5432, ge=1, le=65535)
    postgres_database: str = "orqentis"
    # The database role of the app: for a managed identity, the identity's name (id-<prefix>-reader).
    postgres_user: str | None = None
    postgres_schema: str = Field(default="orqentis", pattern=r"^[a-z_][a-z0-9_]{0,62}$")
    postgres_auth: PostgresAuth = PostgresAuth.ENTRA
    postgres_scope: str = PUBLIC_POSTGRES_SCOPE
    # Run every connection as this role (SET ROLE), for an operator who connects as a member of the administrator group
    # and acts as the application's role (the group has no rights on the application's tables itself): `orqentis audit
    # start-epoch` after a restore, and the storage tests as a non-superuser. Needs the GRANT in postgres-bootstrap.sql.
    postgres_set_role: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.@-]{1,63}$")
    postgres_ssl_mode: PostgresSslMode = PostgresSslMode.VERIFY_FULL
    postgres_ssl_root_cert: Path | None = None  # extra CA bundle; unset: the system trust store
    # Connections per replica. Burstable B1ms allows roughly 50 in total, so replicas x pool_max must stay well below.
    postgres_pool_min: int = Field(default=1, ge=0, le=100)
    postgres_pool_max: int = Field(default=8, ge=2, le=100)  # migrations hold a lock on one connection
    postgres_command_timeout_seconds: float = Field(default=30.0, gt=0, le=600)
    # Apply pending schema migrations at start, behind a database advisory lock (safe with several replicas).
    # false: run `orqentis db migrate` as a separate step; the app then refuses to start on an older schema.
    postgres_migrate_on_start: bool = True
    # Evidence, reports and audit anchors live in Blob storage (Entra auth, no account keys). The URL is a storage
    # account blob endpoint, or a OneLake one (then the container is the workspace and the prefix the item path).
    evidence_blob_url: str | None = None
    evidence_container: str = "evidence"
    evidence_path_prefix: str = ""
    audit_anchor_container: str = "audit-anchors"
    audit_anchor_path_prefix: str = ""
    # Epoch markers (audit epochs opened after a restore): the application can only READ this container; an operator
    # group writes it (docs/operations/restore.md).
    audit_epoch_container: str = Field(default="audit-epochs", pattern=r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")

    # Agent / reasoning
    reasoner: ReasonerKind = ReasonerKind.RULE_BASED
    llm_provider: LlmProvider = LlmProvider.AZURE_OPENAI
    azure_openai_endpoint: str | None = None
    azure_openai_deployment: str | None = None
    azure_openai_api_version: str | None = None
    foundry_project_endpoint: str | None = None
    foundry_model: str | None = None
    agent_max_tool_calls: int = Field(default=12, ge=0, le=50)
    # Prices (USD per million tokens) behind the estimated-cost metric orqentis.llm.cost. 0 means "not configured":
    # tokens are still counted, no cost is emitted. Take the numbers from the model's price list; they are not defaults
    # here because they differ per model, region and agreement.
    llm_input_cost_per_million_tokens: float = Field(default=0.0, ge=0, le=100_000)
    llm_output_cost_per_million_tokens: float = Field(default=0.0, ge=0, le=100_000)

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
    # Anchor the audit chain head (sequence + hash) to an immutable blob (postgres backend).
    scheduler_audit_anchor_enabled: bool = True
    scheduler_audit_anchor_interval_minutes: int = Field(default=15, ge=1, le=24 * 60)
    # Verifies the audit hash chain (and the immutable anchor, once the storage layer has one); failure raises an alert.
    scheduler_audit_verify_enabled: bool = True
    scheduler_audit_verify_interval_minutes: int = Field(default=60, ge=1, le=7 * 24 * 60)
    investigation_max_concurrency: int = Field(default=2, ge=1, le=16)
    investigation_timeout_seconds: int = Field(default=600, ge=30, le=6 * 3600)
    # Durable investigation jobs (postgres backend): every replica polls for queued jobs and claims them; a claim that
    # is not renewed within the TTL (a crashed replica) is released for another replica to take over.
    investigation_poll_interval_seconds: float = Field(default=2.0, gt=0, le=60)
    investigation_claim_ttl_seconds: float = Field(default=120.0, ge=10, le=3600)

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

    @field_validator("expected_forbidden_collectors")
    @classmethod
    def _known_collectors(cls, value: str) -> str:
        names = [n.strip() for n in value.split(",") if n.strip()]
        unknown = sorted(set(names) - set(COLLECTOR_NAMES))
        if unknown:
            raise ValueError(f"unknown collector(s) {', '.join(unknown)}; known: {', '.join(COLLECTOR_NAMES)}")
        return ",".join(dict.fromkeys(names))

    @field_validator("onelake_base_url")
    @classmethod
    def _valid_onelake_base_url(cls, value: str) -> str:
        parts = urlsplit(value.strip())
        if parts.scheme != "https" or not parts.hostname or parts.path.strip("/") or parts.query or parts.fragment:
            raise ValueError("must be an https origin such as https://onelake.dfs.fabric.microsoft.com")
        return f"https://{parts.netloc}"

    @field_validator("fabric_scope", "powerbi_scope", "postgres_scope", "kusto_scope", "onelake_scope")
    @classmethod
    def _valid_scope(cls, value: str) -> str:
        scope = value.strip()
        resource, _, suffix = scope.rpartition("/")
        if suffix != ".default" or urlsplit(resource).scheme not in ("https", "api") or not urlsplit(resource).netloc:
            raise ValueError(
                "must be a resource URI ending in /.default, e.g. https://api.fabric.microsoft.com/.default"
            )
        return scope

    @field_validator("contracts_blob_name")
    @classmethod
    def _valid_blob_name(cls, value: str) -> str:
        name = value.strip()
        if not name or len(name) > 1024 or name.startswith("/") or "\\" in name or ".." in name.split("/"):
            raise ValueError("must be a relative blob name without '..' segments or backslashes, e.g. estate.json")
        return name

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
        if self.postgres_pool_min > self.postgres_pool_max:
            raise ValueError("ORQENTIS_POSTGRES_POOL_MIN must be <= ORQENTIS_POSTGRES_POOL_MAX")
        if self.investigation_claim_ttl_seconds < self.investigation_poll_interval_seconds * 3:
            raise ValueError(
                "ORQENTIS_INVESTIGATION_CLAIM_TTL_SECONDS must be at least three times "
                "ORQENTIS_INVESTIGATION_POLL_INTERVAL_SECONDS"
            )
        if "refresh_source" not in self.model_fields_set:
            # Synthetic telemetry has no monitoring database: its refresh history is replayed in the Power BI shape.
            self.refresh_source = (
                RefreshSource.WORKSPACE_MONITORING if self.fabric_mode is FabricMode.REST else RefreshSource.POWERBI_API
            )
        if "sla_max_telemetry_lag_minutes" not in self.model_fields_set:
            self.sla_max_telemetry_lag_minutes = 30 if self.fabric_mode is FabricMode.REST else 0
        if (self.capacity_metrics_workspace_id is None) != (self.capacity_metrics_dataset_id is None):
            raise ValueError(
                "ORQENTIS_CAPACITY_METRICS_WORKSPACE_ID and ORQENTIS_CAPACITY_METRICS_DATASET_ID go together: "
                "set both (the Capacity Metrics app is read through that one semantic model) or neither"
            )
        if self.storage_backend is StorageBackend.POSTGRES:
            self._check_postgres()
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

    def _check_postgres(self) -> None:
        missing = [
            f"ORQENTIS_{name.upper()}"
            for name, value in (
                ("postgres_host", self.postgres_host),
                ("postgres_user", self.postgres_user),
                ("evidence_blob_url", self.evidence_blob_url),
            )
            if not value
        ]
        if missing:
            raise ValueError(f"ORQENTIS_STORAGE_BACKEND=postgres requires {', '.join(missing)}")
        if self.deployment_environment != "local" and (
            self.postgres_auth is not PostgresAuth.ENTRA or self.postgres_ssl_mode is not PostgresSslMode.VERIFY_FULL
        ):
            # The Entra access token is the database password: a connection that does not verify the server's
            # certificate and host name (require) would hand it to anyone who can intercept the connection.
            raise ValueError(
                "ORQENTIS_POSTGRES_AUTH=none and ORQENTIS_POSTGRES_SSL_MODE=disable or require are only permitted "
                "when deployment_environment=local (a throwaway test container)"
            )
        parts = urlsplit(self.evidence_blob_url or "")
        if parts.scheme != "https" and not (parts.scheme == "http" and _is_loopback(parts.hostname or "")):
            raise ValueError("ORQENTIS_EVIDENCE_BLOB_URL must be an https URL (http only for a loopback emulator)")

    def check_storage_for_environment(self) -> None:
        """Deployed environments keep their state in PostgreSQL and Blob, never in local files.

        Two replicas, or one replica that restarts, on SQLite files would each hold a different truth (and a
        different audit chain). This is checked where the process starts (``orqentis serve``) and when the
        container is built, so a misconfigured deployment stops instead of quietly losing data.
        """
        if self.deployment_environment != "local" and self.storage_backend is StorageBackend.LOCAL:
            raise ValueError(
                f"ORQENTIS_DEPLOYMENT_ENVIRONMENT={self.deployment_environment} requires "
                "ORQENTIS_STORAGE_BACKEND=postgres: local SQLite and file storage are for development only"
            )

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
    def expected_forbidden_collector_names(self) -> tuple[str, ...]:
        return tuple(n for n in self.expected_forbidden_collectors.split(",") if n)

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
