"""``orqentis contracts validate | scaffold | schema``."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from pydantic import ValidationError
from rich.console import Console

from orqentis import bootstrap
from orqentis.config.settings import FabricMode, Settings
from orqentis.contracts.catalog import CatalogSnapshot
from orqentis.contracts.diagnostics import read_text
from orqentis.contracts.errors import ContractsError
from orqentis.contracts.scaffold import ScaffoldOptions, scaffold_contracts
from orqentis.contracts.schema import contracts_json_schema, render_contracts_schema
from orqentis.contracts.validate import ValidationReport, validate_text
from orqentis.fabric.errors import FabricError

_REGENERATE = "regenerate it with: orqentis contracts schema --output {path}"


def add_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    contracts = sub.add_parser("contracts", help="Author and check the estate contracts file")
    actions = contracts.add_subparsers(dest="contracts_command", required=True)

    validate = actions.add_parser(
        "validate", help="Check a contracts file: schema, duplicate ids, references (non-zero exit on errors)"
    )
    validate.add_argument("path", type=Path)
    validate.add_argument(
        "--catalog",
        type=Path,
        default=None,
        help="Workspaces and items to check references against (a scaffold --catalog-output file or a full estate "
        "file). A full estate file is its own catalog.",
    )
    validate.add_argument("--tenant-id", default=None, help="Require every record to carry this tenant id")
    validate.add_argument("--strict", action="store_true", help="Treat warnings (unreviewed drafts) as errors")

    scaffold = actions.add_parser(
        "scaffold",
        help="Draft a contracts file from a live read-only listing (a draft for human review, never applied)",
    )
    scaffold.add_argument("--output", type=Path, required=True, help="Where to write the draft contracts file")
    scaffold.add_argument("--catalog-output", type=Path, default=None, help="Also write the listing, for validate")
    scaffold.add_argument("--tenant-id", default=None, help="Fabric tenant id (default: ORQENTIS_TENANT_ID)")
    scaffold.add_argument(
        "--workspace", action="append", default=[], metavar="ID_OR_NAME", help="Only this workspace (repeatable)"
    )
    scaffold.add_argument("--max-runs", type=int, default=5, help="Recent runs read per pipeline (default 5)")
    scaffold.add_argument("--schema-ref", default=None, help='Value for "$schema" in the draft (path or URL)')
    scaffold.add_argument("--force", action="store_true", help="Overwrite existing output files")
    scaffold.add_argument(
        "--scanner",
        action="store_true",
        help="Add lineage from the Power BI admin Scanner API. Interactive only: you sign in as a Fabric administrator",
    )
    scaffold.add_argument(
        "--scanner-client-id", default=None, help="Public-client app registration to sign in with (optional)"
    )

    schema = actions.add_parser("schema", help="Print or check the JSON Schema of the contracts file")
    schema.add_argument("--output", type=Path, default=None, help="Write the schema to this file")
    schema.add_argument("--check", type=Path, default=None, help="Exit non-zero if this file differs from the model")


def run(args: argparse.Namespace, console: Console) -> int:
    if args.contracts_command == "validate":
        return _validate(args, console)
    if args.contracts_command == "scaffold":
        return asyncio.run(_scaffold(args, console))
    if args.contracts_command == "schema":
        return _schema(args, console)
    return 2


def _say(console: Console, text: str) -> None:
    console.print(text, markup=False, highlight=False, soft_wrap=True)


def _print_report(report: ValidationReport, console: Console) -> None:
    for violation in report.violations:
        _say(console, violation.format(report.path))
    _say(
        console,
        f"{report.path}: {len(report.errors)} error(s), {len(report.warnings)} warning(s)"
        + ("" if report.catalog_checked else " (references to Fabric items were not checked against a catalog)"),
    )


# ----------------------------------------------------------------------------------------------- validate
def _catalog_for(path: Path, text: str, explicit: Path | None) -> CatalogSnapshot | None:
    if explicit is not None:
        return CatalogSnapshot.load(explicit)
    try:
        peek = json.loads(text)
    except ValueError:
        return None  # a syntax error is reported by validate itself
    if isinstance(peek, dict) and ("items" in peek or "workspaces" in peek):
        return CatalogSnapshot.load(path)  # a full estate file is its own catalog
    return None


def _validate(args: argparse.Namespace, console: Console) -> int:
    try:
        text = read_text(args.path)
        catalog = _catalog_for(args.path, text, args.catalog)
    except ContractsError as exc:
        _say(console, str(exc))
        return 1
    report = validate_text(text, args.path, catalog=catalog, tenant_id=args.tenant_id)
    _print_report(report, console)
    return 0 if report.ok(strict=args.strict) else 1


# ----------------------------------------------------------------------------------------------- scaffold
def _refuse_overwrite(paths: list[Path], console: Console) -> bool:
    existing = [p for p in paths if p.exists()]
    for path in existing:
        _say(console, f"{path} already exists; choose another --output or pass --force")
    return bool(existing)


def _interactive() -> bool:
    """True when a person is at a terminal (the Scanner needs a signed-in administrator)."""
    return sys.stdin.isatty() and sys.stdout.isatty()


async def _scaffold(args: argparse.Namespace, console: Console) -> int:
    outputs = [args.output, *([args.catalog_output] if args.catalog_output else [])]
    if not args.force and _refuse_overwrite(outputs, console):
        return 1
    if args.scanner and not _interactive():
        _say(
            console,
            "--scanner is interactive: run it in a terminal and sign in as a Fabric administrator. "
            "It is not available to scripts or to the application.",
        )
        return 2
    overrides: dict[str, object] = {"fabric_mode": FabricMode.REST}
    if args.tenant_id:
        overrides["tenant_id"] = args.tenant_id
    try:
        settings = Settings(**overrides)  # type: ignore[arg-type]
    except ValidationError as exc:
        _say(console, "Settings are invalid for scaffold (set ORQENTIS_TENANT_ID or pass --tenant-id):")
        for error in exc.errors(include_url=False, include_input=False, include_context=False):
            _say(console, f"  {'.'.join(str(p) for p in error['loc'])}: {error['msg']}")
        return 2

    fabric = await bootstrap.build_discovery_client(settings)
    scanner = (
        await bootstrap.build_lineage_scanner(settings, client_id=args.scanner_client_id) if args.scanner else None
    )
    try:
        result = await scaffold_contracts(
            fabric,
            ScaffoldOptions(
                tenant_id=settings.tenant_id,
                timezone=settings.default_timezone,
                workspaces=tuple(args.workspace),
                max_runs_per_pipeline=max(1, args.max_runs),
                schema_ref=args.schema_ref,
            ),
            scanner=scanner,
        )
    except FabricError as exc:
        _say(console, f"Fabric could not be read ({exc.error_code or type(exc).__name__}): {exc.message}")
        return 1
    finally:
        await fabric.aclose()
        if scanner is not None:
            await scanner.aclose()

    text = result.contracts_json()
    report = validate_text(text, args.output, catalog=result.catalog, tenant_id=settings.tenant_id)
    if report.errors:  # a scaffold bug, never the operator's mistake: write nothing
        _print_report(report, console)
        return 1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(text, encoding="utf-8", newline="\n")
    if args.catalog_output:
        args.catalog_output.parent.mkdir(parents=True, exist_ok=True)
        args.catalog_output.write_text(result.catalog.to_json(), encoding="utf-8", newline="\n")

    counts = ", ".join(
        f"{n} {name.replace('_', ' ')}" for name, n in result.stats.items() if name != "activity_runs_read"
    )
    _say(console, f"Wrote draft {args.output}: {counts}.")
    for note in result.notes:
        _say(console, f"note: {note}")
    _say(console, f"{len(report.warnings)} entries still need review (inferred environments, TODO placeholders).")
    _say(
        console,
        "This is a draft for human review. Edit it, then check it with: orqentis contracts validate "
        f"{args.output}" + (f" --catalog {args.catalog_output}" if args.catalog_output else ""),
    )
    return 0


# ------------------------------------------------------------------------------------------------ schema
def _schema(args: argparse.Namespace, console: Console) -> int:
    rendered = render_contracts_schema()
    if args.check is not None:
        try:
            committed = json.loads(read_text(args.check))
        except (ContractsError, ValueError):
            _say(console, f"{args.check} is missing or not valid JSON; {_REGENERATE.format(path=args.check)}")
            return 1
        if committed != contracts_json_schema():
            _say(console, f"{args.check} is out of date; {_REGENERATE.format(path=args.check)}")
            return 1
        _say(console, f"{args.check} matches the model.")
        return 0
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8", newline="\n")
        _say(console, f"Wrote {args.output}")
        return 0
    sys.stdout.write(rendered)
    return 0
