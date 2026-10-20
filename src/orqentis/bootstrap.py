"""Composition root: wires ports to adapters from :class:`Settings`.

This is the only module that knows which concrete adapters are in use (mock vs REST Fabric,
rule-based vs Agent Framework reasoner, local stores vs cloud stores).
"""

from __future__ import annotations

import contextlib
import inspect
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING

from orqentis.actions.gateway import ActionGateway
from orqentis.agents.investigator import FabricOpsInvestigator, TokenPrices
from orqentis.agents.reasoning import Reasoner
from orqentis.agents.rule_based import RuleBasedReasoner
from orqentis.approvals.provider import ApprovalProvider, LocalApprovalProvider
from orqentis.approvals.service import ApprovalService
from orqentis.config.clock import Clock, FixedClock, SystemClock
from orqentis.config.settings import ClockMode, FabricMode, PostgresAuth, ReasonerKind, Settings, StorageBackend
from orqentis.contracts import ContractsNotConfiguredError, EstateContracts
from orqentis.contracts.lineage import LineageScanner
from orqentis.contracts.source import (
    ContractsHolder,
    ContractsSource,
    FileContractsSource,
    load_contracts_from,
)
from orqentis.domain.incidents import Incident
from orqentis.fabric.fixtures import ScenarioFixture, load_estate, load_history, load_scenario
from orqentis.fabric.mock import MockFabricClient
from orqentis.fabric.ports import FabricClient, FabricWriteClient
from orqentis.fabric.write import DisabledFabricWriteClient, MockFabricWriteClient
from orqentis.incidents.classification import ErrorClassifier
from orqentis.incidents.confidence import ConfidenceCalculator
from orqentis.incidents.detection import DetectionEngine
from orqentis.incidents.detection_runner import DetectionRunner
from orqentis.incidents.history import IncidentHistory
from orqentis.incidents.investigation import InvestigationService
from orqentis.incidents.service import IncidentService
from orqentis.incidents.sla import SLAEvaluator
from orqentis.incidents.sla_sweep import SLASweeper
from orqentis.incidents.workflows.pipeline_failure import PipelineFailureWorkflow, WorkflowDependencies
from orqentis.notifications.factory import build_notification_service
from orqentis.notifications.port import Notifier
from orqentis.notifications.sink import NOOP_SINK, NotificationService
from orqentis.observability.anchors import AnchorableAudit, AuditAnchorStore, AuditAnchorTask
from orqentis.observability.audit import AuditTrail, LocalAuditLog
from orqentis.observability.epochs import EpochMarkerStore
from orqentis.observability.logging import get_logger
from orqentis.observability.metrics import Metrics, get_metrics
from orqentis.policies.engine import PolicyEngine
from orqentis.reports.builder import ReportBuilder
from orqentis.reports.publisher import ReportPublisher
from orqentis.runbooks.catalog import RunbookCatalog
from orqentis.runtime.audit_verification import AuditAnchorVerifier, AuditVerificationTask
from orqentis.runtime.collection import CollectionService
from orqentis.runtime.investigations import AutoInvestigationTask, InvestigationJobRunner
from orqentis.runtime.job_store import JobStore
from orqentis.runtime.lease import InProcessLeaderLease, LeaderLease
from orqentis.runtime.mutex import InProcessMutex, Mutex
from orqentis.runtime.replica_sync import ReplicaCatalogSync
from orqentis.runtime.scheduler import JobContext, JobSpec, Scheduler
from orqentis.storage.dependency_store import SqliteDependencyStore
from orqentis.storage.event_store import EventStore, LocalEventStore
from orqentis.storage.evidence_store import EvidenceStore, LocalEvidenceStore
from orqentis.storage.incidents import IncidentRepository, SqliteIncidentRepository
from orqentis.storage.sqlite import SqliteDatabase
from orqentis.telemetry.collectors import (
    CapacityCollector,
    Collector,
    IngestionResult,
    PipelineRunCollector,
    RefreshCollector,
    StaticEventCollector,
    TelemetryIngestor,
)
from orqentis.tools.mutation_tools import build_mutation_request_tools
from orqentis.tools.read_tools import ReadToolDependencies, build_read_tools
from orqentis.tools.registry import ToolRegistry
from orqentis.topology.catalog import EstateCatalog
from orqentis.topology.environment_audit import EnvironmentChangeRecorder
from orqentis.topology.graph import DependencyGraph, DependencyStore
from orqentis.topology.refresh import CatalogRefresher

if TYPE_CHECKING:  # the adapters import their SDKs; keep them out of the import path of local runs
    from orqentis.storage.blob import BlobStorage
    from orqentis.storage.postgres import PostgresStorage

_log = get_logger(__name__)

# Files and folders the local adapters create inside ``data_dir`` (reset only touches these).
_LOCAL_STATE = ("events.sqlite3", "state.sqlite3", "evidence", "audit", "logs")


@dataclass
class Container:
    settings: Settings
    clock: Clock
    fabric: FabricClient
    write_client: FabricWriteClient
    catalog: EstateCatalog
    graph: DependencyGraph
    event_store: EventStore
    evidence_store: EvidenceStore
    audit: AuditTrail
    runbooks: RunbookCatalog
    policy: PolicyEngine
    incidents: IncidentService
    approvals: ApprovalService
    gateway: ActionGateway
    registry: ToolRegistry
    investigator: FabricOpsInvestigator
    investigations: InvestigationService
    detection: DetectionEngine
    ingestor: TelemetryIngestor
    reports: ReportPublisher
    scenario: ScenarioFixture | None
    collection: CollectionService
    catalog_refresher: CatalogRefresher
    replica_sync: ReplicaCatalogSync
    sla_sweeper: SLASweeper
    investigation_jobs: InvestigationJobRunner
    lease: LeaderLease
    scheduler: Scheduler
    _databases: tuple[SqliteDatabase, ...]
    # PostgreSQL backend: the job that anchors the audit chain head (None with local storage).
    audit_anchoring: AuditAnchorTask | None = None
    # Closers of the PostgreSQL pool, the Blob client and the credential (empty with local storage).
    _closers: tuple[Callable[[], Awaitable[None]], ...] = ()
    # Notifications (ADR-0018): started and stopped with the scheduler by the API runtime. Without a notifier it
    # discards events.
    notifications: NotificationService = NOOP_SINK
    metrics: Metrics = field(default_factory=get_metrics)

    async def collect_and_detect(self) -> tuple[IngestionResult, list[Incident]]:
        """Collect, then run detection. Serialised with the scheduled collection (see ``CollectionService``)."""
        return await self.collection.run()

    async def aclose(self) -> None:
        await self.fabric.aclose()
        for db in self._databases:
            db.close()
        for close in reversed(self._closers):
            await close()


@dataclass
class _Storage:
    """The storage adapters one backend provides (see ``_build_local_storage`` and ``_build_postgres_storage``)."""

    events: EventStore
    incidents: IncidentRepository
    evidence: EvidenceStore
    audit: AuditTrail
    approvals: ApprovalProvider
    dependencies: DependencyStore
    lease: LeaderLease
    mutex: Mutex
    # None: the investigation runner keeps its jobs in memory (one process).
    jobs: JobStore | None = None
    # Set with PostgreSQL: the contracts file as a blob (read-only for the app).
    contracts: ContractsSource | None = None
    # Set with PostgreSQL: where the audit head is anchored, and the chain that is anchored.
    anchors: AuditAnchorStore | None = None
    epochs: EpochMarkerStore | None = None  # read-only for the app: only an operator opens an epoch
    audit_head: AnchorableAudit | None = None
    # Set with PostgreSQL: the scheduled audit verification also checks every anchor and epoch marker (verify_anchor).
    audit_anchor_verifier: AuditAnchorVerifier | None = None
    databases: tuple[SqliteDatabase, ...] = ()
    closers: tuple[Callable[[], Awaitable[None]], ...] = ()


def _build_local_storage(
    settings: Settings, clock: Clock, cleanup: list[Callable[[], Awaitable[None] | None]]
) -> _Storage:
    """SQLite files, filesystem evidence and a JSONL audit log under ``data_dir`` (development, tests, the demo)."""
    state_db = SqliteDatabase(settings.state_db_path)
    cleanup.append(state_db.close)
    events_db = SqliteDatabase(settings.event_store_path)
    cleanup.append(events_db.close)
    return _Storage(
        events=LocalEventStore(events_db),
        incidents=SqliteIncidentRepository(state_db),
        evidence=LocalEvidenceStore(settings.evidence_dir),
        audit=LocalAuditLog(settings.audit_log_path, clock),
        approvals=LocalApprovalProvider(state_db),
        dependencies=SqliteDependencyStore(state_db),
        lease=InProcessLeaderLease(clock),
        mutex=InProcessMutex(),
        databases=(state_db, events_db),
    )


@dataclass
class PostgresSession:
    """An open connection pool to the PostgreSQL system of record plus the Blob storage next to it."""

    storage: PostgresStorage
    blob: BlobStorage
    _closers: tuple[Callable[[], Awaitable[None]], ...]

    async def aclose(self) -> None:
        for close in reversed(self._closers):
            await close()


async def open_postgres_session(
    settings: Settings,
    clock: Clock,
    *,
    migrate: bool | None,
    cleanup: list[Callable[[], Awaitable[None] | None]] | None = None,
) -> PostgresSession:
    """Connect to PostgreSQL and Blob storage with the deployment's Entra identity (ADR-0008, ADR-0017).

    One Entra credential serves both: the access token for PostgreSQL is the connection password (renewed per new
    connection from the provider's cache), and the Blob client signs requests with the same credential. It follows
    ``llm_auth_mode`` when set, because these are Azure resources of the deployment (the reader managed identity), not
    the Fabric tenant, which a cross-tenant service principal reaches.

    ``migrate``: True applies pending migrations, False insists that none are pending, None does neither
    (``orqentis db status``).
    """
    from orqentis.fabric.rest.auth import AzureIdentityTokenProvider, build_async_credential
    from orqentis.storage.blob import BlobStorage
    from orqentis.storage.postgres import PostgresConfig, PostgresStorage

    assert settings.postgres_host and settings.postgres_user and settings.evidence_blob_url  # checked by Settings
    track = cleanup if cleanup is not None else []
    credential = build_async_credential(settings, settings.llm_auth_mode or settings.auth_mode)
    tokens = AzureIdentityTokenProvider(credential)
    track.append(tokens.close)  # closes the credential
    try:
        blob = BlobStorage.open(
            settings.evidence_blob_url,
            credential,
            evidence_container=settings.evidence_container,
            evidence_prefix=settings.evidence_path_prefix,
            anchor_container=settings.audit_anchor_container,
            anchor_prefix=settings.audit_anchor_path_prefix,
            contracts_container=settings.contracts_container,
            contracts_blob_name=settings.contracts_blob_name,
            epoch_container=settings.audit_epoch_container,
        )
        track.append(blob.close)

        async def database_token() -> str:
            try:
                return await tokens.get_token(settings.postgres_scope)
            except Exception as exc:
                # A ConnectionError is retried by the pool's connect loop (a hiccup of the identity endpoint).
                raise ConnectionError(f"Could not get an Entra token for PostgreSQL: {type(exc).__name__}") from exc

        config = PostgresConfig(
            host=settings.postgres_host,
            port=settings.postgres_port,
            user=settings.postgres_user,
            database=settings.postgres_database,
            schema=settings.postgres_schema,
            auth="entra" if settings.postgres_auth is PostgresAuth.ENTRA else "none",
            ssl_mode=settings.postgres_ssl_mode.value,
            ssl_root_cert=settings.postgres_ssl_root_cert,
            pool_min=settings.postgres_pool_min,
            pool_max=settings.postgres_pool_max,
            command_timeout_seconds=settings.postgres_command_timeout_seconds,
            set_role=settings.postgres_set_role,
        )
        pg = await PostgresStorage.open(
            config,
            database_token if settings.postgres_auth is PostgresAuth.ENTRA else None,
            clock,
            anchors=blob.anchors,
            epochs=blob.epochs,
            # Deployed environments run on the system clock: the database's clock then judges approval expiry and
            # stamps audit records, so a replica with a skewed clock cannot change either.
            database_clock=settings.clock is ClockMode.SYSTEM,
            migrate=migrate,
        )
    except BaseException:
        if cleanup is None:
            for close in reversed(track):
                with contextlib.suppress(Exception):
                    outcome = close()
                    if inspect.isawaitable(outcome):
                        await outcome
        raise
    track.append(pg.close)
    return PostgresSession(pg, blob, (tokens.close, blob.close, pg.close))


async def _build_postgres_storage(
    settings: Settings, clock: Clock, cleanup: list[Callable[[], Awaitable[None] | None]]
) -> _Storage:
    """The PostgreSQL system of record and Blob evidence; safe at two or more replicas."""
    session = await open_postgres_session(settings, clock, migrate=settings.postgres_migrate_on_start, cleanup=cleanup)
    pg, blob = session.storage, session.blob
    return _Storage(
        events=pg.events,
        incidents=pg.incidents,
        evidence=blob.evidence,
        audit=pg.audit,
        approvals=pg.approvals,
        dependencies=pg.dependencies,
        lease=pg.lease,
        mutex=pg.mutex,
        jobs=pg.jobs,
        anchors=blob.anchors,
        epochs=blob.epochs,
        contracts=blob.contracts,
        audit_head=pg.audit,
        audit_anchor_verifier=pg.audit,
        closers=session._closers,
    )


async def _build_storage(
    settings: Settings, clock: Clock, cleanup: list[Callable[[], Awaitable[None] | None]]
) -> _Storage:
    if settings.storage_backend is StorageBackend.POSTGRES:
        return await _build_postgres_storage(settings, clock, cleanup)
    return _build_local_storage(settings, clock, cleanup)


def reset_local_state(settings: Settings) -> None:
    for name in _LOCAL_STATE:
        path = settings.data_dir / name
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()
        for suffix in ("-wal", "-shm"):
            sidecar = settings.data_dir / f"{name}{suffix}"
            if sidecar.exists():
                sidecar.unlink()


def _build_reasoner(settings: Settings) -> tuple[Reasoner, Reasoner | None]:
    rule_based = RuleBasedReasoner(timezone=settings.default_timezone)
    if settings.reasoner is ReasonerKind.RULE_BASED:
        return rule_based, None
    from orqentis.agents.agent_framework import AgentFrameworkReasoner, build_chat_client_factory
    from orqentis.fabric.rest.auth import build_sync_credential

    factory = build_chat_client_factory(
        provider=settings.llm_provider.value,
        credential=build_sync_credential(settings),
        azure_openai_endpoint=settings.azure_openai_endpoint,
        azure_openai_deployment=settings.azure_openai_deployment,
        azure_openai_api_version=settings.azure_openai_api_version,
        foundry_project_endpoint=settings.foundry_project_endpoint,
        foundry_model=settings.foundry_model,
        max_tool_calls=settings.agent_max_tool_calls,
    )
    model = settings.azure_openai_deployment or settings.foundry_model
    return AgentFrameworkReasoner(factory, model=model), rule_based


def _contracts_source(settings: Settings, from_storage: ContractsSource | None) -> ContractsSource:
    """An explicit ``ORQENTIS_CONTRACTS_PATH`` wins; otherwise the storage backend's contracts blob (deployed)."""
    if settings.contracts_path is not None:
        return FileContractsSource(settings.contracts_path)
    if from_storage is not None:
        return from_storage
    raise ContractsNotConfiguredError()


async def _build_fabric(
    settings: Settings, contracts_from_storage: ContractsSource | None
) -> tuple[FabricClient, EstateContracts, ScenarioFixture | None, ContractsHolder | None]:
    if settings.fabric_mode is FabricMode.MOCK:
        estate = load_estate(settings.fixtures_dir)
        scenario = load_scenario(settings.fixtures_dir, settings.scenario)
        return MockFabricClient(estate, scenario), estate, scenario, None
    source = _contracts_source(settings, contracts_from_storage)
    from orqentis.fabric.rest.auth import build_token_provider
    from orqentis.fabric.rest.client import RestFabricClient

    # A ContractsError names where the document came from and every violation; the build is retried until it loads.
    contracts, document = await load_contracts_from(source)
    client = RestFabricClient.from_settings(settings, contracts, build_token_provider(settings))
    return client, contracts, None, ContractsHolder(source, contracts, document, client.apply_contracts)


async def build_discovery_client(settings: Settings) -> FabricClient:
    """A read-only REST client with *no* contracts, for ``orqentis contracts scaffold``.

    With no contracts the client classifies environments by name inference only, which is what the scaffold
    records as ``inferred``. It carries the same read-only port as the application's client.
    """
    from orqentis.fabric.rest.auth import build_token_provider
    from orqentis.fabric.rest.client import RestFabricClient

    return RestFabricClient.from_settings(settings, EstateContracts(), build_token_provider(settings))


async def build_lineage_scanner(settings: Settings, *, client_id: str | None = None) -> LineageScanner:
    """The Power BI admin Scanner, signed in interactively as a Fabric administrator.

    Only ``orqentis contracts scaffold --scanner`` calls this. The application never does: there is no auth
    mode that reaches it, and the scanner is not part of :class:`Container`.
    """
    from orqentis.fabric.rest.auth import InteractiveTokenProvider
    from orqentis.fabric.rest.http import FabricHttp
    from orqentis.fabric.rest.scanner import PowerBiScanner

    tokens = InteractiveTokenProvider(settings, client_id=client_id)
    http = FabricHttp(tokens, timeout=settings.http_timeout_seconds, max_retries=settings.http_max_retries)
    return PowerBiScanner(http, settings.powerbi_api_base_url, on_close=tokens.close)


async def build_container(
    settings: Settings,
    *,
    reset: bool = False,
    reasoner: Reasoner | None = None,
    write_client: FabricWriteClient | None = None,
    clock: Clock | None = None,
    notifier: Notifier | None = None,
    metrics: Metrics | None = None,
) -> Container:
    """Wire the application. If anything fails part-way, what was already opened is closed again.

    The API retries a failed build in the background, so leaving SQLite connections or HTTP clients behind would
    leak a set of them (and keep the data files locked) on every attempt.
    """
    cleanup: list[Callable[[], Awaitable[None] | None]] = []
    try:
        return await _build_container(
            settings,
            reset=reset,
            reasoner=reasoner,
            write_client=write_client,
            clock=clock,
            notifier=notifier,
            cleanup=cleanup,
            metrics=metrics or get_metrics(),
        )
    except BaseException:
        for close in reversed(cleanup):
            with contextlib.suppress(Exception):
                outcome = close()
                if inspect.isawaitable(outcome):
                    await outcome
        raise


async def _build_container(
    settings: Settings,
    *,
    reset: bool,
    reasoner: Reasoner | None,
    write_client: FabricWriteClient | None,
    clock: Clock | None,
    notifier: Notifier | None,
    cleanup: list[Callable[[], Awaitable[None] | None]],
    metrics: Metrics,
) -> Container:
    settings.check_storage_for_environment()
    if reset:
        if settings.storage_backend is not StorageBackend.LOCAL:
            raise ValueError("reset deletes local files only; it is refused for the PostgreSQL storage backend")
        reset_local_state(settings)
    settings.data_dir.mkdir(parents=True, exist_ok=True)

    storage: _Storage | None = None
    if settings.fabric_mode is FabricMode.REST:
        # The contracts file may live in the storage backend (a blob), so storage comes first. A real tenant is
        # always on the system clock, so nothing here waits for the scenario.
        clock = clock if clock is not None else SystemClock()
        storage = await _build_storage(settings, clock, cleanup)
    fabric, contracts, scenario, contracts_holder = await _build_fabric(
        settings, storage.contracts if storage else None
    )
    cleanup.append(fabric.aclose)
    if clock is None:
        clock = (
            FixedClock(scenario.scenario.now)
            if scenario is not None and settings.clock is ClockMode.SCENARIO
            else SystemClock()
        )
    if storage is None:
        storage = await _build_storage(settings, clock, cleanup)
    event_store, repository = storage.events, storage.incidents
    evidence_store, audit = storage.evidence, storage.audit

    recorder: EnvironmentChangeRecorder | None = None
    if contracts.source_sha256 is not None and contracts_holder is not None:
        # rest mode: say which declared environments change a classification, tied to the contracts version in force
        recorder = EnvironmentChangeRecorder(
            contracts_sha256=contracts.source_sha256,
            contracts_path=contracts_holder.document.location,
            contracts_version=contracts_holder.document.version,
        )
        await recorder.bind(audit)
        observe = getattr(fabric, "observe_environment_changes", None)  # the REST adapter; the mock has no contracts
        if callable(observe):
            observe(recorder.observe)  # also reports workspaces and items discovered after this listing
    catalog = await EstateCatalog.load(fabric, contracts)
    if recorder is not None:
        await recorder.record_loaded(catalog)
    dependency_store = storage.dependencies
    await dependency_store.replace_all(await fabric.dependencies.list_dependencies())
    graph = await DependencyGraph.from_store(dependency_store, catalog)

    runbooks = RunbookCatalog.load(settings.runbooks_dir)
    policy = (
        PolicyEngine.from_file(settings.policy_path, clock) if settings.policy_path else PolicyEngine.default(clock)
    )
    classifier = ErrorClassifier()

    if scenario is not None:
        for past in load_history(settings.fixtures_dir):
            if await repository.get(past.incident_id) is None:
                await repository.save(past)

    notifications = build_notification_service(settings, audit, notifier=notifier, metrics=metrics)
    incidents = IncidentService(repository, event_store, audit, clock, notifications, metrics=metrics)
    approvals = ApprovalService(
        storage.approvals,
        incidents,
        audit,
        clock,
        ttl=timedelta(minutes=settings.approval_ttl_minutes),
        notifications=notifications,
        metrics=metrics,
    )
    if write_client is None:
        write_client = (
            MockFabricWriteClient(clock)
            if settings.enable_write_actions and settings.fabric_mode is FabricMode.MOCK
            else DisabledFabricWriteClient()
        )
    gateway = ActionGateway(
        policy,
        approvals,
        write_client,
        incidents,
        audit,
        clock,
        write_enabled=settings.enable_write_actions,
        notifications=notifications,
    )

    history = IncidentHistory(repository, classifier, timezone=settings.default_timezone)
    read_deps = ReadToolDependencies(
        fabric=fabric,
        catalog=catalog,
        graph=graph,
        event_store=event_store,
        history=history,
        runbooks=runbooks,
        sla=SLAEvaluator(event_store, clock),
        clock=clock,
    )
    registry = ToolRegistry(
        [*build_read_tools(read_deps), *build_mutation_request_tools(gateway, catalog, clock)],
        audit,
        clock,
        metrics=metrics,
    )

    primary: Reasoner
    fallback: Reasoner | None
    if reasoner is not None:
        # Injected reasoners (e.g. tests, custom LLM backends) still fall back to the deterministic one.
        fallback = (
            None if reasoner.name == RuleBasedReasoner.name else RuleBasedReasoner(timezone=settings.default_timezone)
        )
        primary = reasoner
    else:
        primary, fallback = _build_reasoner(settings)
    investigator = FabricOpsInvestigator(
        primary,
        registry,
        tool_allowlist=policy.tool_allowlist,
        audit=audit,
        clock=clock,
        max_tool_calls=settings.agent_max_tool_calls,
        fallback=fallback,
        metrics=metrics,
        token_prices=TokenPrices(
            input_per_million=settings.llm_input_cost_per_million_tokens,
            output_per_million=settings.llm_output_cost_per_million_tokens,
        ),
    )
    reports = ReportPublisher(ReportBuilder(clock, timezone=settings.default_timezone), evidence_store)
    workflow = PipelineFailureWorkflow(
        WorkflowDependencies(
            registry=registry,
            incidents=incidents,
            evidence_store=evidence_store,
            investigator=investigator,
            classifier=classifier,
            catalog=catalog,
            graph=graph,
            history=history,
            runbooks=runbooks,
            confidence=ConfidenceCalculator(),
            policy=policy,
            gateway=gateway,
            approvals=approvals,
            reports=reports,
            clock=clock,
            auto_request_approval=settings.auto_request_approval,
        )
    )
    investigations = InvestigationService([workflow], incidents, evidence_store, audit, clock)

    collectors: list[Collector] = [PipelineRunCollector(fabric, catalog), RefreshCollector(fabric, catalog)]
    if scenario is not None:
        # Capacity metrics and Delta schema events have no public REST API; in mock mode they are replayed.
        collectors += [CapacityCollector(fabric, catalog), StaticEventCollector(scenario.events)]
    ingestor = TelemetryIngestor(
        collectors,
        event_store,
        clock,
        lookback=timedelta(hours=settings.collection_max_lookback_hours),
        overlap=timedelta(minutes=settings.collection_overlap_minutes),
        metrics=metrics,
    )
    detection = DetectionEngine(incidents, clock, events=event_store)

    # Autonomous runtime: the scheduler is built here but started by the API lifespan (see runtime/service.py).
    detection_runner = DetectionRunner(event_store, detection, mutex=storage.mutex)
    collection = CollectionService(ingestor, detection_runner, metrics=metrics, clock=clock)
    catalog_refresher = CatalogRefresher(
        fabric,
        contracts,
        catalog,
        graph,
        dependency_store,
        incidents,
        audit,
        clock,
        contracts_holder=contracts_holder,
        recorder=recorder,
        metrics=metrics,
    )
    sla_sweeper = SLASweeper(
        catalog, SLAEvaluator(event_store, clock), graph, repository, event_store, detection_runner, clock
    )
    investigation_timeout = timedelta(seconds=settings.investigation_timeout_seconds)
    investigation_jobs = InvestigationJobRunner(
        investigations,
        incidents,
        audit,
        clock,
        store=storage.jobs,
        max_concurrency=settings.investigation_max_concurrency,
        timeout=investigation_timeout,
        poll_interval=timedelta(seconds=settings.investigation_poll_interval_seconds),
        claim_ttl=timedelta(seconds=settings.investigation_claim_ttl_seconds),
        metrics=metrics,
    )
    auto_investigation = AutoInvestigationTask(
        incidents,
        investigation_jobs,
        clock,
        catalog=catalog,
        batch_size=settings.scheduler_auto_investigate_batch_size,
        max_attempts=settings.scheduler_auto_investigate_max_attempts,
        orphan_after=investigation_timeout + timedelta(minutes=5),
    )

    # Jobs receive a JobContext (lease holder + fencing token) and run inside a Fence (runtime/fencing.py): with
    # PostgreSQL every write they make is checked against the lease in its own transaction, so a deposed leader's late
    # write is refused. The local adapters belong to one process and ignore it.
    audit_verification = AuditVerificationTask(audit, metrics=metrics, anchor=storage.audit_anchor_verifier)

    # (the hash chain and, with PostgreSQL, every anchor and epoch marker; see PostgresAuditTrail.verify_anchor)
    async def collect_job(ctx: JobContext) -> None:
        try:
            await collection.run()
        finally:
            # Expiry by the clock changes no stored row, so the gauge is refreshed here as well as on every change.
            await approvals.publish_pending_metric()

    async def auto_investigate_job(ctx: JobContext) -> None:
        await auto_investigation.run()

    async def sla_sweep_job(ctx: JobContext) -> None:
        await sla_sweeper.sweep()

    async def catalog_refresh_job(ctx: JobContext) -> None:
        await catalog_refresher.refresh()

    lease = storage.lease
    job_specs = [
        JobSpec(
            "collect-detect",
            timedelta(minutes=settings.scheduler_collect_interval_minutes),
            collect_job,
            enabled=settings.scheduler_collect_enabled,
        ),
        JobSpec(
            "auto-investigate",
            timedelta(minutes=settings.scheduler_auto_investigate_interval_minutes),
            auto_investigate_job,
            enabled=settings.scheduler_auto_investigate_enabled,
        ),
        # The next two start one interval after the scheduler: the first collection must have run before SLAs
        # are judged, and the catalog was loaded a moment ago.
        JobSpec(
            "sla-sweep",
            timedelta(minutes=settings.scheduler_sla_sweep_interval_minutes),
            sla_sweep_job,
            enabled=settings.scheduler_sla_sweep_enabled,
            run_immediately=False,
        ),
        JobSpec(
            "catalog-refresh",
            timedelta(minutes=settings.scheduler_catalog_refresh_interval_minutes),
            catalog_refresh_job,
            enabled=settings.scheduler_catalog_refresh_enabled,
            run_immediately=False,
        ),
    ]

    async def audit_verify_job(ctx: JobContext) -> None:
        await audit_verification.run()

    job_specs.append(
        JobSpec(
            "audit-verify",
            timedelta(minutes=settings.scheduler_audit_verify_interval_minutes),
            audit_verify_job,
            enabled=settings.scheduler_audit_verify_enabled,
        )
    )
    anchoring: AuditAnchorTask | None = None
    if storage.anchors is not None and storage.audit_head is not None:
        # Copies the audit chain head (sequence + hash) to an immutable blob, so that rewriting the database cannot
        # also rewrite what the chain is checked against (observability/anchors.py).
        anchoring = anchor_task = AuditAnchorTask(
            storage.audit_head,
            storage.anchors,
            clock,
            epochs=storage.epochs,
            stale_after=2 * timedelta(minutes=settings.scheduler_audit_anchor_interval_minutes),
        )

        async def audit_anchor_job(ctx: JobContext) -> None:
            await anchor_task.run()

        job_specs.append(
            JobSpec(
                "audit-anchor",
                timedelta(minutes=settings.scheduler_audit_anchor_interval_minutes),
                audit_anchor_job,
                enabled=settings.scheduler_audit_anchor_enabled,
            )
        )
    scheduler = Scheduler(
        job_specs,
        lease,
        clock,
        lease_ttl=timedelta(seconds=settings.scheduler_lease_ttl_seconds),
        tick_interval=timedelta(seconds=settings.scheduler_tick_seconds),
        start_delay=timedelta(seconds=settings.scheduler_startup_delay_seconds),
        default_job_timeout=timedelta(minutes=settings.scheduler_job_timeout_minutes),
        audit=audit,
        metrics=metrics,
    )
    await approvals.publish_pending_metric()

    # Every replica keeps its own memory (catalog, graph, contracts) current; the leader's scheduled job does it there.
    replica_sync = ReplicaCatalogSync(
        catalog_refresher,
        lambda: scheduler.is_leader,
        timedelta(minutes=settings.scheduler_catalog_refresh_interval_minutes),
    )

    _log.info(
        "container ready",
        extra={
            "fabric_mode": settings.fabric_mode.value,
            "reasoner": primary.name,
            "write_enabled": settings.enable_write_actions,
        },
    )
    return Container(
        settings=settings,
        clock=clock,
        fabric=fabric,
        write_client=write_client,
        catalog=catalog,
        graph=graph,
        event_store=event_store,
        evidence_store=evidence_store,
        audit=audit,
        runbooks=runbooks,
        policy=policy,
        incidents=incidents,
        approvals=approvals,
        gateway=gateway,
        registry=registry,
        investigator=investigator,
        investigations=investigations,
        detection=detection,
        ingestor=ingestor,
        reports=reports,
        scenario=scenario,
        collection=collection,
        catalog_refresher=catalog_refresher,
        replica_sync=replica_sync,
        sla_sweeper=sla_sweeper,
        investigation_jobs=investigation_jobs,
        lease=lease,
        scheduler=scheduler,
        audit_anchoring=anchoring,
        _databases=storage.databases,
        _closers=storage.closers,
        notifications=notifications,
        metrics=metrics,
    )
