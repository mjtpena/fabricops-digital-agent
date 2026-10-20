"""Make declared environment overrides visible: a log line and an audit record for every one that changes a
classification, tied to the exact contracts file (SHA-256) that caused it.

A declaration that lowers a PROD workspace (``Sales [PROD]`` declared DEV) silently lowers incident severity and
switches production-only policy rules (``POL-002``, ``POL-020``) off for it. It is a legitimate correction when
the name is wrong, so it is allowed, but it must never be invisible.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from orqentis.domain.common import Environment
from orqentis.domain.principals import SYSTEM_PRINCIPAL
from orqentis.observability.audit import AuditTrail
from orqentis.observability.logging import get_logger
from orqentis.tools.untrusted import sanitize_text
from orqentis.topology.catalog import EstateCatalog
from orqentis.topology.environment import EnvironmentSource, infer_environment, is_downgrade

_log = get_logger(__name__)

CONTRACTS_LOADED_ACTION = "contracts.loaded"
OVERRIDE_ACTION = "contracts.environment_override"


@dataclass(frozen=True)
class EnvironmentChange:
    """A declared environment that differs from what the name (or the workspace) would have given."""

    kind: str  # "workspace" | "item" | "capacity"
    node_id: str
    display_name: str
    inferred: Environment
    resolved: Environment
    source: str

    @property
    def downgrade(self) -> bool:
        return is_downgrade(self.inferred, self.resolved)


def environment_changes(catalog: EstateCatalog) -> list[EnvironmentChange]:
    """Every workspace, item and capacity whose declared environment differs from its inferred one."""
    changes: list[EnvironmentChange] = []
    for workspace in catalog.workspaces.values():
        source = str(workspace.metadata.get("environment_source", ""))
        inferred = infer_environment(workspace.display_name)
        if source.startswith("declared_") and workspace.environment is not inferred:
            changes.append(
                EnvironmentChange(
                    "workspace", workspace.id, workspace.display_name, inferred, workspace.environment, source
                )
            )
    for item in catalog.items.values():
        owner = catalog.workspaces.get(item.workspace_id)
        inherited = owner.environment if owner is not None else Environment.UNKNOWN
        if (
            item.metadata.get("environment_source") == EnvironmentSource.DECLARED_ITEM
            and item.environment is not inherited
        ):
            changes.append(
                EnvironmentChange("item", item.id, item.display_name, inherited, item.environment, "declared_item")
            )
    for capacity in catalog.capacities.values():
        inferred = infer_environment(capacity.display_name)
        if capacity.metadata.get("environment_source") == EnvironmentSource.DECLARED_CAPACITY and (
            capacity.environment is not inferred
        ):
            changes.append(
                EnvironmentChange(
                    "capacity", capacity.id, capacity.display_name, inferred, capacity.environment, "declared_capacity"
                )
            )
    return changes


def _drafts_ignored(catalog: EstateCatalog) -> int:
    ignored = EnvironmentSource.DRAFT_IGNORED
    return sum(
        1
        for node in (*catalog.workspaces.values(), *catalog.items.values(), *catalog.capacities.values())
        if node.metadata.get("environment_source") == ignored
    )


async def record_environment_changes(
    catalog: EstateCatalog, audit: AuditTrail, *, contracts_sha256: str, contracts_path: Path | None
) -> list[EnvironmentChange]:
    """Log and audit every environment override, then one ``contracts.loaded`` record with the file's SHA-256."""
    changes = environment_changes(catalog)
    downgrades = [c for c in changes if c.downgrade]
    for change in changes:
        details = {
            "kind": change.kind,
            "id": change.node_id,
            "display_name": sanitize_text(change.display_name, max_length=120),
            "inferred": change.inferred.value,
            "resolved": change.resolved.value,
            "source": change.source,
            "downgrade": change.downgrade,
            "contracts_sha256": contracts_sha256,
        }
        _log.log(
            logging.WARNING if change.downgrade else logging.INFO,
            "environment override applied",
            extra={f"environment_{key}": value for key, value in details.items()},
        )
        await audit.record(actor=SYSTEM_PRINCIPAL.id, action=OVERRIDE_ACTION, outcome="applied", details=details)
    summary = {
        "contracts_sha256": contracts_sha256,
        "contracts_path": str(contracts_path) if contracts_path is not None else None,
        "environment_overrides": len(changes),
        "environment_downgrades": len(downgrades),
        "unreviewed_environment_entries_ignored": _drafts_ignored(catalog),
    }
    await audit.record(actor=SYSTEM_PRINCIPAL.id, action=CONTRACTS_LOADED_ACTION, outcome="loaded", details=summary)
    return changes
