"""Validation of a contracts file: schema, then internal consistency, then (optionally) the real catalog.

``validate_text`` is what ``orqentis contracts validate`` runs; ``load_contracts`` runs the same checks
without a catalog and raises on errors. Messages name ids and JSON pointers, never values from the file.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from orqentis.contracts.catalog import CatalogSnapshot
from orqentis.contracts.diagnostics import parse_json, pydantic_violations, read_document, violation
from orqentis.contracts.errors import ContractViolation, Severity
from orqentis.contracts.locator import Locations
from orqentis.contracts.model import CONTRACT_SECTIONS, ESTATE_ONLY_KEYS, EstateContracts
from orqentis.domain.common import Environment
from orqentis.domain.estate import DataProduct, ExternalSource
from orqentis.domain.sla import SLA
from orqentis.domain.topology import Dependency
from orqentis.topology.environment import EnvironmentResolver, infer_environment, is_downgrade, name_matches


@dataclass(frozen=True)
class ValidationReport:
    path: Path | None
    violations: tuple[ContractViolation, ...]
    contracts: EstateContracts | None  # None when the file is not valid JSON or does not match the schema
    catalog_checked: bool = False
    source_sha256: str | None = None  # of the file's bytes, when read from a file

    @property
    def errors(self) -> tuple[ContractViolation, ...]:
        return tuple(v for v in self.violations if v.severity == "error")

    @property
    def warnings(self) -> tuple[ContractViolation, ...]:
        return tuple(v for v in self.violations if v.severity == "warning")

    def ok(self, *, strict: bool = False) -> bool:
        return not self.errors and not (strict and self.warnings)


@dataclass
class _Checker:
    contracts: EstateContracts
    locations: Locations
    catalog: CatalogSnapshot | None
    tenant_id: str | None
    out: list[ContractViolation] = field(default_factory=list)

    def add(self, pointer: str, message: str, severity: Severity = "error") -> None:
        self.out.append(violation(self.locations, pointer, message, severity))

    # ---------------------------------------------------------------------------------------- helpers
    def _line(self, pointer: str) -> str:
        position = self.locations.find(pointer)
        return f"line {position[0]}" if position else "earlier in the file"

    def _duplicates(self, section: str, ids: Iterable[str]) -> None:
        first: dict[str, int] = {}
        for index, node_id in enumerate(ids):
            if node_id in first:
                self.add(
                    f"/{section}/{index}/id",
                    f"duplicate id '{node_id}' (first declared at {self._line(f'/{section}/{first[node_id]}/id')})",
                )
            else:
                first[node_id] = index

    # ----------------------------------------------------------------------------------------- checks
    def run(self) -> list[ContractViolation]:
        c = self.contracts
        declared = {s.id for s in c.external_sources} | {d.id for d in c.data_products}
        fabric_items = set(self.catalog.items) if self.catalog else set()
        self._ids(fabric_items)
        self._environments()
        unchecked = self._dependencies(declared, fabric_items)
        unchecked += self._slas(declared, fabric_items)
        unchecked += self._data_products_and_sources(declared, fabric_items)
        if unchecked and self.catalog is None:
            self.add(
                "",
                f"{unchecked} reference(s) to Fabric items could not be checked because no catalog was supplied "
                "(use --catalog, or validate a full estate file)",
                "warning",
            )
        self._tenants()
        self._drafts()
        if not any(getattr(c, section) for section in CONTRACT_SECTIONS):
            self.add("", "the file declares nothing (all sections are empty)", "warning")
        return self.out

    def _ids(self, fabric_items: set[str]) -> None:
        c = self.contracts
        self._duplicates("external_sources", (s.id for s in c.external_sources))
        self._duplicates("data_products", (d.id for d in c.data_products))
        self._duplicates("dependencies", (d.id for d in c.dependencies))
        self._duplicates("slas", (s.id for s in c.slas))
        source_ids = {s.id for s in c.external_sources}
        for index, product in enumerate(c.data_products):
            if product.id in source_ids:
                self.add(
                    f"/data_products/{index}/id",
                    f"id '{product.id}' is also an external source; sources and data products share one id space",
                )
        nodes = (
            ("external_sources", [s.id for s in c.external_sources]),
            ("data_products", [d.id for d in c.data_products]),
        )
        for section, ids in nodes:
            for index, node_id in enumerate(ids):
                if node_id in fabric_items:
                    self.add(
                        f"/{section}/{index}/id",
                        f"id '{node_id}' is also a Fabric item in the catalog; the declared node would be shadowed",
                    )

    def _environments(self) -> None:
        seen: dict[tuple[str, str], int] = {}
        for index, declaration in enumerate(self.contracts.environments):
            pointer = f"/environments/{index}"
            kind, value = next(
                (k, v)
                for k, v in (
                    ("workspace_id", declaration.workspace_id),
                    ("workspace_name_pattern", declaration.workspace_name_pattern),
                    ("item_id", declaration.item_id),
                    ("capacity_id", declaration.capacity_id),
                )
                if v is not None
            )
            if (kind, value) in seen:
                first = self._line(f"/environments/{seen[(kind, value)]}")
                self.add(f"{pointer}/{kind}", f"duplicate {kind} '{value}' (first declared at {first})")
            else:
                seen[(kind, value)] = index
            if declaration.environment is Environment.UNKNOWN:
                self.add(
                    f"{pointer}/environment",
                    "environment is UNKNOWN, so this entry is NOT enforced (the name decides): "
                    "declare DEV, TEST or PROD",
                    "warning",
                )
            if declaration.inferred:
                self.add(
                    pointer,
                    "drafted from naming conventions and NOT enforced until reviewed: confirm the environment, "
                    'then remove "inferred" (or set it to false)',
                    "warning",
                )
            pattern = declaration.workspace_name_pattern
            if pattern is not None and declaration.enforced and not any(ch not in "*?" for ch in pattern):
                self.add(
                    f"{pointer}/workspace_name_pattern",
                    "a catch-all pattern: it overrides name inference for EVERY workspace in the tenant. "
                    "Declare the workspaces or a narrower pattern instead",
                    "warning",
                )
            if self.catalog is None:
                continue
            if declaration.workspace_id is not None and declaration.workspace_id not in self.catalog.workspaces:
                self.add(f"{pointer}/workspace_id", f"workspace '{declaration.workspace_id}' is not in the catalog")
            if declaration.item_id is not None and declaration.item_id not in self.catalog.items:
                self.add(f"{pointer}/item_id", f"item '{declaration.item_id}' is not in the catalog")
            if pattern is not None and not any(
                name_matches(pattern, w.display_name) for w in self.catalog.workspaces.values()
            ):
                self.add(
                    f"{pointer}/workspace_name_pattern", "the pattern matches no workspace in the catalog", "warning"
                )
        self._downgrades()

    def _downgrades(self) -> None:
        """Warn about every enforced declaration that lowers an environment the names say is PROD.

        A PROD workspace declared TEST or DEV gets a lower incident severity and escapes production-only policy
        rules (``POL-002``, ``POL-020``), so it is flagged for the reviewer (and fails ``--strict``).
        """
        if self.catalog is None:
            return
        resolver = EnvironmentResolver(self.contracts.environments)
        accepted = {i for i, d in enumerate(self.contracts.environments) if d.accepts_downgrade}
        lowered: dict[int, list[str]] = defaultdict(list)
        for workspace in self.catalog.workspaces.values():
            resolved = resolver.resolve_workspace(workspace.id, workspace.display_name)
            if resolved.declaration_index is not None and is_downgrade(
                infer_environment(workspace.display_name), resolved.environment
            ):
                lowered[resolved.declaration_index].append(workspace.id)
        for item in self.catalog.items.values():
            owner = self.catalog.workspaces.get(item.workspace_id)
            workspace_environment = resolver.workspace(owner.id, owner.display_name) if owner else Environment.UNKNOWN
            resolved_item = resolver.resolve_item(item.id, workspace_environment)
            if resolved_item.declaration_index is not None and is_downgrade(
                workspace_environment, resolved_item.environment
            ):
                lowered[resolved_item.declaration_index].append(item.id)
        for index, ids in sorted(lowered.items()):
            if index in accepted:
                continue
            shown = ", ".join(f"'{i}'" for i in sorted(ids)[:3]) + (", ..." if len(ids) > 3 else "")
            self.add(
                f"/environments/{index}",
                f"lowers {len(ids)} PROD workspace(s) or item(s) ({shown}): their incidents become less severe and "
                "production-only policy rules (POL-002, POL-020) stop applying to them. Confirm this is intended",
                "warning",
            )

    def _dependencies(self, declared: set[str], fabric_items: set[str]) -> int:
        unchecked: set[str] = set()
        seen: dict[tuple[str, str, str], int] = {}
        for index, edge in enumerate(self.contracts.dependencies):
            pointer = f"/dependencies/{index}"
            key = (edge.source_item_id, edge.target_item_id, edge.dependency_type.value)
            if key in seen:
                self.add(
                    pointer,
                    f"duplicate edge {edge.source_item_id} -[{edge.dependency_type.value}]-> {edge.target_item_id} "
                    f"(first declared at {self._line(f'/dependencies/{seen[key]}')}); each edge may appear once",
                )
            else:
                seen[key] = index
            if edge.source_item_id == edge.target_item_id:
                self.add(pointer, f"'{edge.source_item_id}' depends on itself")
            endpoints = (("source_item_id", edge.source_item_id), ("target_item_id", edge.target_item_id))
            for field_name, node_id in endpoints:
                if node_id in declared or node_id in fabric_items:
                    continue
                if self.catalog is not None:
                    self.add(
                        f"{pointer}/{field_name}",
                        f"'{node_id}' is not a declared source or data product, nor an item in the catalog",
                    )
                else:
                    unchecked.add(node_id)
        return len(unchecked)

    def _slas(self, declared: set[str], fabric_items: set[str]) -> int:
        products = {d.id for d in self.contracts.data_products}
        unchecked: set[str] = set()
        for index, sla in enumerate(self.contracts.slas):
            pointer = f"/slas/{index}"
            if sla.data_product_id not in products:
                self.add(
                    f"{pointer}/data_product_id",
                    f"data product '{sla.data_product_id}' is not declared in data_products",
                )
            if sla.freshness_item_id not in declared and sla.freshness_item_id not in fabric_items:
                if self.catalog is not None:
                    self.add(f"{pointer}/freshness_item_id", f"item '{sla.freshness_item_id}' is not in the catalog")
                else:
                    unchecked.add(sla.freshness_item_id)
            try:
                ZoneInfo(sla.timezone)
            except (KeyError, ValueError, OSError):  # unknown key, malformed key, or a directory name
                self.add(f"{pointer}/timezone", "not a valid IANA time zone (for example Australia/Sydney)")
            days = sla.applies_on_weekdays
            if not days or any(not 1 <= d <= 7 for d in days) or len(set(days)) != len(days):
                self.add(
                    f"{pointer}/applies_on_weekdays",
                    "must be a non-empty list of distinct ISO weekdays, 1 (Monday) to 7 (Sunday)",
                )
        return len(unchecked)

    def _data_products_and_sources(self, declared: set[str], fabric_items: set[str]) -> int:
        unchecked: set[str] = set()
        c = self.contracts
        if self.catalog is not None:
            resolver = EnvironmentResolver(c.environments)
            located: tuple[tuple[str, Sequence[ExternalSource | DataProduct]], ...] = (
                ("external_sources", c.external_sources),
                ("data_products", c.data_products),
            )
            for section, records in located:
                for index, record in enumerate(records):
                    workspace = self.catalog.workspaces.get(record.workspace_id)
                    if workspace is None:
                        self.add(
                            f"/{section}/{index}/workspace_id",
                            f"workspace '{record.workspace_id}' is not in the catalog",
                        )
                        continue
                    resolved = resolver.workspace(workspace.id, workspace.display_name)
                    if Environment.UNKNOWN not in (record.environment, resolved) and record.environment != resolved:
                        self.add(
                            f"/{section}/{index}/environment",
                            f"'{record.id}' is {record.environment.value} but its workspace resolves to "
                            f"{resolved.value}",
                            "warning",
                        )
        for index, product in enumerate(c.data_products):
            for position, item_id in enumerate(product.backing_item_ids):
                if item_id in declared or item_id in fabric_items:
                    continue
                if self.catalog is not None:
                    self.add(
                        f"/data_products/{index}/backing_item_ids/{position}", f"item '{item_id}' is not in the catalog"
                    )
                else:
                    unchecked.add(item_id)
        return len(unchecked)

    def _tenants(self) -> None:
        c = self.contracts
        sections: tuple[tuple[str, Sequence[ExternalSource | DataProduct | Dependency | SLA]], ...] = (
            ("external_sources", c.external_sources),
            ("data_products", c.data_products),
            ("dependencies", c.dependencies),
            ("slas", c.slas),
        )
        records: list[tuple[str, str]] = [
            (f"/{section}/{i}/tenant_id", record.tenant_id)
            for section, items in sections
            for i, record in enumerate(items)
        ]
        if self.tenant_id is not None:
            for pointer, tenant in records:
                if tenant != self.tenant_id:
                    self.add(pointer, f"tenant_id '{tenant}' does not match the expected tenant '{self.tenant_id}'")
            return
        counts = Counter(tenant for _, tenant in records)
        if len(counts) > 1:
            self.add(
                "", f"records carry {len(counts)} different tenant_id values ({', '.join(sorted(counts))})", "warning"
            )

    def _drafts(self) -> None:
        c = self.contracts
        drafted: tuple[tuple[str, Sequence[ExternalSource | DataProduct | SLA]], ...] = (
            ("external_sources", c.external_sources),
            ("data_products", c.data_products),
            ("slas", c.slas),
        )
        for section, items in drafted:
            for index, record in enumerate(items):
                if record.metadata.get("draft") is True:
                    self.add(
                        f"/{section}/{index}",
                        f"draft placeholder '{record.id}': fill it in or delete it, then remove metadata.draft",
                        "warning",
                    )


def check_contracts(
    contracts: EstateContracts,
    locations: Locations | None = None,
    *,
    catalog: CatalogSnapshot | None = None,
    tenant_id: str | None = None,
) -> list[ContractViolation]:
    """Internal-consistency checks (and catalog checks when ``catalog`` is given) on parsed contracts."""
    return _Checker(contracts, locations or Locations(), catalog, tenant_id).run()


def validate_text(
    text: str,
    path: Path | None = None,
    *,
    catalog: CatalogSnapshot | None = None,
    tenant_id: str | None = None,
) -> ValidationReport:
    raw: Any
    raw, locations, violations = parse_json(text)
    if not violations and not isinstance(raw, dict):
        violations.append(ContractViolation("", "the contracts file must be a JSON object"))
    if violations:
        return ValidationReport(path, tuple(violations), None, catalog is not None)
    payload = {k: v for k, v in raw.items() if k not in ESTATE_ONLY_KEYS}
    try:
        contracts = EstateContracts.model_validate(payload)
    except ValidationError as exc:
        return ValidationReport(path, tuple(pydantic_violations(exc, locations)), None, catalog is not None)
    found = check_contracts(contracts, locations, catalog=catalog, tenant_id=tenant_id)
    return ValidationReport(path, tuple(found), contracts, catalog is not None)


def validate_file(
    path: Path, *, catalog: CatalogSnapshot | None = None, tenant_id: str | None = None
) -> ValidationReport:
    """Read and validate ``path``. Raises :class:`ContractsFileNotFoundError` if it cannot be read."""
    text, digest = read_document(path)
    report = validate_text(text, path, catalog=catalog, tenant_id=tenant_id)
    if report.contracts is not None:
        report.contracts.with_source_sha256(digest)
    return replace(report, source_sha256=digest)
