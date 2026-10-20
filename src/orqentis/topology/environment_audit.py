"""Make declared environment overrides visible: a log line and an audit record for every one that changes a
classification, tied to the exact contracts file (SHA-256) that caused it.

A declaration that lowers a PROD workspace (``Sales [PROD]`` declared DEV) silently lowers incident severity and
switches production-only policy rules (``POL-002``, ``POL-020``) off for it. It is a legitimate correction when
the name is wrong, so it is allowed, but it must never be invisible.

The REST adapter reports a change the first time it maps a node (at startup, and again for any workspace, item or
capacity discovered later, for example by a catalog refresh), and the recorder writes it once per node.
"""

from __future__ import annotations

import logging
from pathlib import Path

from orqentis.domain.principals import SYSTEM_PRINCIPAL
from orqentis.observability.audit import AuditTrail
from orqentis.observability.logging import get_logger
from orqentis.tools.untrusted import sanitize_text
from orqentis.topology.catalog import EstateCatalog
from orqentis.topology.environment import EnvironmentChange, EnvironmentSource

_log = get_logger(__name__)

CONTRACTS_LOADED_ACTION = "contracts.loaded"
OVERRIDE_ACTION = "contracts.environment_override"


def _drafts_ignored(catalog: EstateCatalog) -> int:
    ignored = EnvironmentSource.DRAFT_IGNORED.value
    return sum(
        1
        for node in (*catalog.workspaces.values(), *catalog.items.values(), *catalog.capacities.values())
        if node.metadata.get("environment_source") == ignored
    )


class EnvironmentChangeRecorder:
    """Logs and audits each environment override once, with the SHA-256 of the contracts file that declared it."""

    def __init__(
        self, *, contracts_sha256: str, contracts_path: Path | str | None, contracts_version: str | None = None
    ) -> None:
        self._sha256 = contracts_sha256
        self._path = contracts_path
        self._version = contracts_version
        self._audit: AuditTrail | None = None
        self._seen: dict[tuple[str, str], EnvironmentChange] = {}
        self._pending: list[EnvironmentChange] = []

    @property
    def changes(self) -> list[EnvironmentChange]:
        return list(self._seen.values())

    def reset(self, *, contracts_sha256: str, contracts_path: Path | str | None, contracts_version: str | None) -> None:
        """A new version of the contracts is in force: its overrides are reported afresh, tied to its SHA-256."""
        self._sha256 = contracts_sha256
        self._path = contracts_path
        self._version = contracts_version
        self._seen.clear()
        self._pending.clear()

    async def bind(self, audit: AuditTrail) -> None:
        """Attach the audit trail and write whatever was observed before it existed."""
        self._audit = audit
        pending, self._pending = self._pending, []
        for change in pending:
            await self._write(change)

    async def observe(self, change: EnvironmentChange) -> None:
        key = (change.kind, change.node_id)
        if key in self._seen:
            return  # once per node, however often it is listed again
        self._seen[key] = change
        if self._audit is None:
            self._pending.append(change)
        else:
            await self._write(change)

    async def _write(self, change: EnvironmentChange) -> None:
        assert self._audit is not None
        details = {
            "kind": change.kind,
            "id": change.node_id,
            "display_name": sanitize_text(change.display_name, max_length=120),
            "inferred": change.inferred.value,
            "resolved": change.resolved.value,
            "source": change.source,
            "downgrade": change.downgrade,
            "contracts_sha256": self._sha256,
        }
        _log.log(
            logging.WARNING if change.downgrade else logging.INFO,
            "environment override applied",
            extra={f"environment_{key}": value for key, value in details.items()},
        )
        await self._audit.record(actor=SYSTEM_PRINCIPAL.id, action=OVERRIDE_ACTION, outcome="applied", details=details)

    async def record_loaded(self, catalog: EstateCatalog) -> None:
        """One ``contracts.loaded`` record: which file (by digest) is in force and what it changed so far."""
        assert self._audit is not None, "bind() the audit trail first"
        changes = self.changes
        await self._audit.record(
            actor=SYSTEM_PRINCIPAL.id,
            action=CONTRACTS_LOADED_ACTION,
            outcome="loaded",
            details={
                "contracts_sha256": self._sha256,
                "contracts_path": str(self._path) if self._path is not None else None,
                "contracts_version": self._version,
                "environment_overrides": len(changes),
                "environment_downgrades": sum(1 for c in changes if c.downgrade),
                "unreviewed_environment_entries_ignored": _drafts_ignored(catalog),
            },
        )
