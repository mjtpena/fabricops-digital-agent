"""Ports and adapters (invariant 10): the storage SDKs are imported only by their adapter packages."""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "orqentis"


def imported_modules(path: Path) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module)
    return modules


def importers(prefix: str) -> set[str]:
    found = set()
    for path in SRC.rglob("*.py"):
        if any(m == prefix or m.startswith(prefix + ".") for m in imported_modules(path)):
            found.add(path.relative_to(SRC).as_posix())
    return found


def test_the_postgresql_driver_is_imported_only_by_the_postgres_adapter_package() -> None:
    offenders = {p for p in importers("asyncpg") if not p.startswith("storage/postgres/")}
    assert offenders == set(), f"asyncpg must stay inside orqentis.storage.postgres: {sorted(offenders)}"
    assert importers("asyncpg")  # the check itself still sees the adapter


def test_the_blob_sdk_is_imported_only_by_the_blob_adapter_package() -> None:
    offenders = {p for p in importers("azure.storage") if not p.startswith("storage/blob/")}
    assert offenders == set(), f"azure.storage must stay inside orqentis.storage.blob: {sorted(offenders)}"
    assert importers("azure.storage")


def test_the_domain_and_the_runtime_do_not_import_the_adapter_packages() -> None:
    allowed = {"bootstrap.py", "db_commands.py", "cli.py"}
    for prefix in ("orqentis.storage.postgres", "orqentis.storage.blob"):
        offenders = {
            p
            for p in importers(prefix)
            if p not in allowed and not p.startswith(("storage/postgres/", "storage/blob/"))
        }
        assert offenders == set(), f"{prefix} is for the composition root: {sorted(offenders)}"
