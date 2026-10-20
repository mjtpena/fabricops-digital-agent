"""Command line entry point: ``orqentis demo | serve | scenarios | runbooks | audit | db | contracts``."""

from __future__ import annotations

import argparse
import asyncio
import io
import sys
from collections.abc import Sequence
from pathlib import Path
from zoneinfo import ZoneInfo

from rich.console import Console
from rich.markdown import Markdown
from rich.markup import escape
from rich.table import Table

from orqentis import __version__
from orqentis.actions.gateway import ActionBlockedError
from orqentis.bootstrap import Container, build_container, reset_local_state
from orqentis.config.settings import FabricMode, Settings, StorageBackend
from orqentis.contracts.parser import add_contracts_parser
from orqentis.domain.principals import AGENT_PRINCIPAL, Principal, PrincipalKind
from orqentis.fabric.fixtures import list_scenarios
from orqentis.observability.logging import configure_logging
from orqentis.observability.tracing import configure_tracing
from orqentis.reports.markdown import render_markdown
from orqentis.runbooks.loader import RunbookValidationError, load_runbooks
from orqentis.tools.registry import ToolContext

DEMO_OPERATOR = Principal(
    id="demo-operator@contoso.com",
    display_name="Demo operator",
    kind=PrincipalKind.HUMAN,
    roles=frozenset({"FabricOps.Operator"}),
)


def _settings(**overrides: object) -> Settings:
    return Settings(**overrides)  # type: ignore[arg-type]


def _observability(settings: Settings, *, stream_logs: bool) -> None:
    configure_logging(settings.log_level, settings.log_format, settings.log_file_path, stream=stream_logs)
    secret = settings.applicationinsights_connection_string
    configure_tracing(
        console=settings.otel_console_exporter,
        appinsights_connection_string=secret.get_secret_value() if secret else None,
    )


def _utf8_stdio() -> None:
    """Write UTF-8 even when stdout is a pipe on Windows (cp1252 cannot encode the report's box characters)."""
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper) and stream.encoding.lower().replace("-", "") != "utf8":
            stream.reconfigure(encoding="utf-8")


def _display(path: Path | None) -> str:
    if path is None:
        return "-"
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def _row(table: Table, number: int, label: str, detail: str) -> None:
    table.add_row(f"{number}.", label, escape(detail))


async def _boundary_check(c: Container, incident_id: str, console: Console) -> None:
    """Show that the agent cannot cause a mutation: request -> approval, execute -> blocked, delete -> denied."""
    incident = await c.incidents.get(incident_id)
    ctx = ToolContext(
        principal=AGENT_PRINCIPAL,
        trace_id=incident.agent_trace_id or "demo",
        incident=incident,
        allowed_tools=c.policy.tool_allowlist,
    )
    rerun = await c.registry.invoke(
        "request_pipeline_rerun",
        {"pipeline_id": incident.primary_item_id, "reason": "Agent attempts a rerun without approval"},
        ctx,
    )
    approval_id = (rerun.data or {}).get("approval_id")
    console.print(
        f"  - Agent calls [bold]request_pipeline_rerun[/] -> [yellow]{rerun.status.value}[/] "
        f"(approval {approval_id}; executed={(rerun.data or {}).get('executed')})"
    )
    if approval_id:
        try:
            await c.gateway.execute(approval_id, principal=AGENT_PRINCIPAL)
        except ActionBlockedError as exc:
            console.print(
                f"  - Agent tries to execute {approval_id} -> [red]BLOCKED[/] {exc.code}: {escape(exc.message)}"
            )
    delete = await c.registry.invoke(
        "request_item_change",
        {
            "action_type": "DELETE_ITEM",
            "target_item_id": incident.primary_item_id,
            "reason": "Agent attempts a prohibited deletion",
        },
        ctx,
    )
    console.print(
        f"  - Agent requests [bold]DELETE_ITEM[/] -> [red]{delete.status.value}[/] "
        f"({escape((delete.data or {}).get('message', '')[:90])})"
    )
    approve = await c.registry.invoke("approve_action", {"approval_id": approval_id}, ctx)
    console.print(f"  - Agent calls a non-allowlisted tool [bold]approve_action[/] -> [red]{approve.status.value}[/]")
    console.print(
        f"  - Write client calls: {len(getattr(c.write_client, 'executed', []))} "
        "(the write capability boundary is closed)"
    )
    await c.incidents.save(incident)
    await c.reports.publish(incident)


async def run_demo(scenario: str, data_dir: Path, *, show_report: bool, console: Console) -> int:
    settings = _settings(data_dir=data_dir, scenario=scenario, fabric_mode=FabricMode.MOCK)
    # Reset before logging opens data_dir/logs: Windows cannot delete a file that is still open.
    reset_local_state(settings)
    _observability(settings, stream_logs=False)
    c = await build_container(settings)
    try:
        assert c.scenario is not None
        tz = ZoneInfo(settings.default_timezone)
        console.rule(f"[bold]Orqentis FabricOps {__version__}[/] · local demo")
        console.print(f"Scenario [bold]{escape(scenario)}[/]: {escape(c.scenario.scenario.title)}")
        console.print(
            f"Mock Fabric estate ({len(c.catalog.workspaces)} workspaces, {len(c.catalog.items)} items) · "
            f"reasoner [bold]{c.investigator.reasoner_name}[/] · clock frozen at "
            f"{c.clock.now().astimezone(tz):%Y-%m-%d %H:%M %Z} · no Fabric credentials used\n"
        )
        steps = Table(show_header=False, box=None, pad_edge=False)
        steps.add_column(justify="right", style="bold cyan")
        steps.add_column(style="bold")
        steps.add_column(overflow="fold")

        ingestion, created = await c.collect_and_detect()
        counts = ", ".join(f"{n} {t}" for t, n in sorted(ingestion.counts().items()))
        _row(steps, 1, "Simulated telemetry loaded", f"{len(ingestion.new_events)} events ({counts})")
        if not created:
            _row(steps, 2, "Incident detected", "no incident (nothing failed in the detection window)")
            console.print(steps)
            return 1
        incident = created[0]
        _row(
            steps,
            2,
            "Incident detected",
            f"{incident.incident_id} · {incident.title} · {incident.severity.value} · {incident.environment.value}",
        )

        incident, trace = await c.investigations.investigate(incident.incident_id, actor=DEMO_OPERATOR)
        workflow_calls = [t for t in trace.tool_calls if t.caller != AGENT_PRINCIPAL.id]
        _row(
            steps,
            3,
            "Evidence collected",
            f"{len(incident.evidence)} evidence items from {len(workflow_calls)} "
            "read-only tool calls (logged, audited, traced)",
        )
        decision = incident.agent_decision
        assert decision is not None
        rc = incident.probable_root_causes[0] if incident.probable_root_causes else None
        conf = incident.confidence
        _row(
            steps,
            4,
            "Investigation executed",
            f"FabricOpsInvestigator ({decision.reasoner}): {len(decision.accepted_finding_ids)} findings accepted, "
            f"{len(decision.rejected_outputs)} rejected\n"
            + (
                f"Root cause {rc.category} · confidence {conf.level.value} ({conf.score:.2f})\n{rc.statement}"
                if rc and conf
                else f"Root cause undetermined · confidence "
                f"{conf.level.value if conf else 'LOW'}\n{incident.insufficient_evidence_reason}"
            ),
        )
        radius = incident.blast_radius
        slas = ", ".join(f"{a.data_product_name} {a.state.value}" for a in incident.sla_impact) or "no SLA affected"
        _row(
            steps,
            5,
            "Dependency impact calculated",
            f"{radius.summary if radius else 'n/a'}"
            + (" · cross-workspace" if radius and radius.cross_workspace else "")
            + f"\nSLA: {slas}",
        )
        top = incident.matched_runbooks[0] if incident.matched_runbooks else None
        _row(
            steps,
            6,
            "Runbook matched",
            f"{top.runbook_id} v{top.version} '{top.title}' (score {top.score:.2f})" if top else "no runbook matched",
        )
        rec = incident.recommended_actions[0] if incident.recommended_actions else None
        if rec:
            actions = "\n".join(
                f"· {a.title} [{a.target_display_name}] -> {a.risk.value if a.risk else '-'}, "
                f"{a.policy_decision.effect.value if a.policy_decision else '-'}"
                + (f" ({a.approval_id})" if a.approval_id else "")
                for a in rec.actions
            )
            _row(
                steps,
                7,
                "Recommendation produced",
                f"{rec.title}\nRisk {rec.risk.value} · approval required: {'yes' if rec.approval_required else 'no'}"
                f" · incident {incident.status.value}\n{actions}",
            )
        report_path = c.evidence_store.local_path(incident.report_uri) if incident.report_uri else None
        _row(steps, 8, "Incident report rendered", _display(report_path))
        verification = await c.audit.verify()
        _row(
            steps,
            9,
            "Audit trace persisted",
            f"{settings.audit_log_path} · {verification.records} records · hash chain "
            f"{'valid' if verification.valid else 'INVALID'}\ntrace "
            f"{settings.evidence_dir / incident.incident_id / f'trace-{trace.trace_id}.json'}",
        )
        console.print(steps)

        console.print("\n[bold]Approval boundary check[/] (the agent attempts mutations directly)")
        await _boundary_check(c, incident.incident_id, console)
        console.print(
            f"\nAPI: [bold]make serve[/] then POST /approvals/<id>/approve as a human operator. "
            f"Logs: {settings.log_file_path}"
        )
        if show_report:
            incident = await c.incidents.get(incident.incident_id)
            console.rule("Incident Report")
            console.print(Markdown(render_markdown(c.reports.build(incident))))
        return 0
    finally:
        await c.aclose()


async def _cmd_notify_test(settings: Settings, console: Console) -> int:
    """Send one test notification through the configured notifier, with the checks a real notification gets."""
    from datetime import UTC, datetime

    from orqentis.domain.common import new_id
    from orqentis.notifications.content import DeepLinks, compose
    from orqentis.notifications.factory import build_notifier
    from orqentis.notifications.model import NotificationEvent, NotificationKind
    from orqentis.notifications.port import DeliveryError

    notifier = build_notifier(settings)
    if notifier is None:
        console.print("ORQENTIS_NOTIFIER is null: no notifier is configured, so there is nothing to test.")
        return 2
    event = NotificationEvent(
        event_id=new_id("test"),
        kind=NotificationKind.TEST,
        occurred_at=datetime.now(UTC),
        incident_id="test",
        headline="FabricOps test notification",
        facts=(("Notifier", notifier.name), ("Destination", notifier.destination), ("Sent by", "orqentis notify-test")),
    )
    message = compose(event, DeepLinks(settings.public_base_url, sign_in_path=settings.ui_sign_in_path))
    try:
        receipt = await notifier.send(message)
    except DeliveryError as exc:
        console.print(
            f"[red]not delivered[/] to {escape(notifier.destination)}: {escape(exc.code)}: {escape(exc.message)}"
        )
        return 1
    console.print(
        f"delivered to {escape(notifier.destination)} (HTTP {receipt.status_code}); "
        f"the card links to {escape(message.link_url)}"
    )
    return 0


def _cmd_scenarios(settings: Settings, console: Console) -> int:
    table = Table("scenario", "expected category", "runbook", "title")
    for info in list_scenarios(settings.fixtures_dir):
        table.add_row(info.name, info.expected_category, info.expected_runbook_id or "-", info.title)
    console.print(table)
    return 0


def _cmd_runbooks(settings: Settings, console: Console) -> int:
    try:
        runbooks = load_runbooks(settings.runbooks_dir)
    except RunbookValidationError as exc:
        console.print(f"[red]{exc}[/]")
        return 1
    table = Table("id", "version", "category", "risk", "approval", "path")
    for rb in runbooks:
        table.add_row(rb.id, rb.version, rb.category, rb.risk.value, str(rb.approval_required), rb.path)
    console.print(table)
    console.print(f"{len(runbooks)} runbooks valid.")
    return 0


async def _cmd_audit(
    settings: Settings,
    console: Console,
    *,
    start_epoch_reason: str | None = None,
    override_verification: str | None = None,
    disregard_anchors: tuple[str, ...] = (),
) -> int:
    from orqentis.config.clock import SystemClock
    from orqentis.observability.audit import LocalAuditLog

    if settings.storage_backend is StorageBackend.POSTGRES:
        from orqentis.db_commands import run_audit_postgres

        return await run_audit_postgres(
            settings,
            console,
            start_epoch_reason=start_epoch_reason,
            override_verification=override_verification,
            disregard_anchors=disregard_anchors,
        )
    if start_epoch_reason is not None:
        console.print(
            "[red]start-epoch applies to the PostgreSQL audit chain only (ORQENTIS_STORAGE_BACKEND=postgres).[/]"
        )
        return 2

    result = await LocalAuditLog(settings.audit_log_path, SystemClock()).verify()
    console.print(result.model_dump())
    return 0 if result.valid else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="orqentis", description="Orqentis FabricOps: AI reliability engineer for Microsoft Fabric estates"
    )
    parser.add_argument("--version", action="version", version=f"orqentis {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    demo = sub.add_parser("demo", help="Run a synthetic incident end to end (no Fabric credentials needed)")
    demo.add_argument("--scenario", default="schema_drift")
    demo.add_argument("--data-dir", type=Path, default=Path("var/demo"))
    demo.add_argument("--no-report", action="store_true", help="Do not print the incident report")
    serve = sub.add_parser("serve", help="Run the REST API (OpenAPI docs at /docs)")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    sub.add_parser("notify-test", help="Send one test notification through the configured notifier")
    sub.add_parser("scenarios", help="List synthetic scenarios")
    sub.add_parser("runbooks", help="Validate and list runbooks")
    audit = sub.add_parser(
        "audit", help="Verify the audit trail hash chain (and every anchor and epoch marker with PostgreSQL)"
    )
    audit.add_argument("--data-dir", type=Path, default=None)
    audit_sub = audit.add_subparsers(dest="audit_command")
    epoch = audit_sub.add_parser(
        "start-epoch",
        help="After a restore: record the loss in the chain and start a new anchor epoch (docs/operations/restore.md)",
    )
    epoch.add_argument("--reason", required=True, help="Why: the restore time and the source server")
    epoch.add_argument(
        "--override-verification",
        metavar="WHY",
        default=None,
        help="Open the epoch although the chain failed verification beyond being behind its anchors (audited)",
    )
    epoch.add_argument(
        "--disregard-anchor",
        metavar="NAME",
        action="append",
        default=[],
        help="Object name of a junk anchor (epoch-NNNN/<sequence>-<hash>.json) that verification should stop "
        "holding against the chain; repeatable; needs --override-verification; recorded in the marker",
    )
    db = sub.add_parser("db", help="PostgreSQL system of record: migrate, status, reseed-counters")
    db.add_argument("db_command", choices=["migrate", "status", "reseed-counters"])
    add_contracts_parser(sub)
    args = parser.parse_args(argv)
    _utf8_stdio()
    console = Console(highlight=False, width=None if sys.stdout.isatty() else 120)

    if args.command == "demo":
        return asyncio.run(run_demo(args.scenario, args.data_dir, show_report=not args.no_report, console=console))
    if args.command == "serve":
        import uvicorn

        from orqentis.api.app import create_app

        settings = _settings()
        try:
            settings.check_bind_host(args.host)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        try:
            settings.check_storage_for_environment()
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        _observability(settings, stream_logs=True)
        uvicorn.run(create_app(settings), host=args.host, port=args.port, log_config=None)
        return 0
    if args.command == "notify-test":
        return asyncio.run(_cmd_notify_test(_settings(), console))
    if args.command == "scenarios":
        return _cmd_scenarios(_settings(), console)
    if args.command == "runbooks":
        return _cmd_runbooks(_settings(), console)
    if args.command == "audit":
        settings = _settings(**({"data_dir": args.data_dir} if args.data_dir else {}))
        reason = args.reason if args.audit_command == "start-epoch" else None
        override = getattr(args, "override_verification", None) if reason is not None else None
        disregard = tuple(getattr(args, "disregard_anchor", ()) or ()) if reason is not None else ()
        return asyncio.run(
            _cmd_audit(
                settings,
                console,
                start_epoch_reason=reason,
                override_verification=override,
                disregard_anchors=disregard,
            )
        )
    if args.command == "db":
        from orqentis.db_commands import run_db

        return asyncio.run(run_db(_settings(), args.db_command, console))
    if args.command == "contracts":
        from orqentis.contracts import commands as contracts_commands  # lazy: `orqentis serve` never loads these

        return contracts_commands.run(args, console)
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
