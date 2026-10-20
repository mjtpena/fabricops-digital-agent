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

from orqentis.actions.gateway import ActionGateway
from orqentis.agents.investigator import FabricOpsInvestigator, TokenPrices
from orqentis.agents.reasoning import Reasoner
from orqentis.agents.rule_based import RuleBasedReasoner
from orqentis.approvals.provider import LocalApprovalProvider
from orqentis.approvals.service import ApprovalService
from orqentis.config.clock import Clock, FixedClock, SystemClock
from orqentis.config.settings import ClockMode, FabricMode, ReasonerKind, Settings
from orqentis.contracts import ContractsNotConfiguredError, EstateContracts, load_contracts
from orqentis.contracts.lineage import LineageScanner
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
from orqentis.observability.audit import LocalAuditLog
from orqentis.observability.logging import get_logger
from orqentis.observability.metrics import Metrics, get_metrics
from orqentis.policies.engine import PolicyEngine
from orqentis.reports.builder import ReportBuilder
from orqentis.reports.publisher import ReportPublisher
from orqentis.runbooks.catalog import RunbookCatalog
from orqentis.runtime.audit_verification import AuditVerificationTask
from orqentis.runtime.collection import CollectionService
from orqentis.runtime.investigations import AutoInvestigationTask, InvestigationJobRunner
from orqentis.runtime.lease import InProcessLeaderLease, LeaderLease
from orqentis.runtime.scheduler import JobContext, JobSpec, Scheduler
from orqentis.storage.dependency_store import SqliteDependencyStore
from orqentis.storage.event_store import LocalEventStore
from orqentis.storage.evidence_store import LocalEvidenceStore
from orqentis.storage.incidents import SqliteIncidentRepository
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
from orqentis.topology.graph import DependencyGraph
from orqentis.topology.refresh import CatalogRefresher

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
    event_store: LocalEventStore
    evidence_store: LocalEvidenceStore
    audit: LocalAuditLog
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
    sla_sweeper: SLASweeper
    investigation_jobs: InvestigationJobRunner
    lease: LeaderLease
    scheduler: Scheduler
    _databases: tuple[SqliteDatabase, ...]
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


async def _build_fabric(settings: Settings) -> tuple[FabricClient, EstateContracts, ScenarioFixture | None]:
    if settings.fabric_mode is FabricMode.MOCK:
        estate = load_estate(settings.fixtures_dir)
        scenario = load_scenario(settings.fixtures_dir, settings.scenario)
        return MockFabricClient(estate, scenario), estate, scenario
    if settings.contracts_path is None:
        raise ContractsNotConfiguredError()
    from orqentis.fabric.rest.auth import build_token_provider
    from orqentis.fabric.rest.client import RestFabricClient

    contracts = load_contracts(settings.contracts_path)  # ContractsError names the path and every violation
    return RestFabricClient.from_settings(settings, contracts, build_token_provider(settings)), contracts, None


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
    if reset:
        reset_local_state(settings)
    settings.data_dir.mkdir(parents=True, exist_ok=True)

    fabric, contracts, scenario = await _build_fabric(settings)
    cleanup.append(fabric.aclose)
    if clock is None:
        clock = (
            FixedClock(scenario.scenario.now)
            if scenario is not None and settings.clock is ClockMode.SCENARIO
            else SystemClock()
        )

    state_db = SqliteDatabase(settings.state_db_path)
    cleanup.append(state_db.close)
    events_db = SqliteDatabase(settings.event_store_path)
    cleanup.append(events_db.close)
    event_store = LocalEventStore(events_db)
    repository = SqliteIncidentRepository(state_db)
    evidence_store = LocalEvidenceStore(settings.evidence_dir)
    audit = LocalAuditLog(settings.audit_log_path, clock)

    recorder: EnvironmentChangeRecorder | None = None
    if contracts.source_sha256 is not None:  # rest mode: say which declared environments change a classification
        recorder = EnvironmentChangeRecorder(
            contracts_sha256=contracts.source_sha256, contracts_path=settings.contracts_path
        )
        await recorder.bind(audit)
        observe = getattr(fabric, "observe_environment_changes", None)  # the REST adapter; the mock has no contracts
        if callable(observe):
            observe(recorder.observe)  # also reports workspaces and items discovered after this listing
    catalog = await EstateCatalog.load(fabric, contracts)
    if recorder is not None:
        await recorder.record_loaded(catalog)
    dependency_store = SqliteDependencyStore(state_db)
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
        LocalApprovalProvider(state_db),
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
    detection_runner = DetectionRunner(event_store, detection)
    collection = CollectionService(ingestor, detection_runner, metrics=metrics, clock=clock)
    catalog_refresher = CatalogRefresher(fabric, contracts, catalog, graph, dependency_store, incidents, audit, clock)
    sla_sweeper = SLASweeper(
        catalog, SLAEvaluator(event_store, clock), graph, repository, event_store, detection_runner, clock
    )
    investigation_timeout = timedelta(seconds=settings.investigation_timeout_seconds)
    investigation_jobs = InvestigationJobRunner(
        investigations,
        incidents,
        audit,
        clock,
        max_concurrency=settings.investigation_max_concurrency,
        timeout=investigation_timeout,
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

    audit_verification = AuditVerificationTask(audit, metrics=metrics)

    # Jobs receive a JobContext (lease holder + fencing token). Nothing here writes through a fenced store yet; the
    # PostgreSQL work will pass it down so a deposed leader's late write is rejected.
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

    async def audit_verify_job(ctx: JobContext) -> None:
        await audit_verification.run()

    lease = InProcessLeaderLease(clock)
    scheduler = Scheduler(
        [
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
            JobSpec(
                "audit-verify",
                timedelta(minutes=settings.scheduler_audit_verify_interval_minutes),
                audit_verify_job,
                enabled=settings.scheduler_audit_verify_enabled,
            ),
        ],
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
        sla_sweeper=sla_sweeper,
        investigation_jobs=investigation_jobs,
        lease=lease,
        scheduler=scheduler,
        _databases=(state_db, events_db),
        notifications=notifications,
        metrics=metrics,
    )
