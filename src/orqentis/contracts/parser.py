"""Argument parser of ``orqentis contracts ...``.

Kept apart from :mod:`orqentis.contracts.commands` so that building the CLI (which ``orqentis serve`` does too)
never imports the scaffold, the Scanner or the Fabric adapters: those load only when a ``contracts`` command runs.
"""

from __future__ import annotations

import argparse
from pathlib import Path


def add_contracts_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
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
