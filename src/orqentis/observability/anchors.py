"""Audit anchors: the head of the hash chain, copied periodically to storage the database cannot rewrite.

A hash chain proves that records were not *altered* without the break being visible, but whoever can rewrite the whole
table can recompute every hash, and whoever can delete the tail leaves a shorter chain that is still internally valid.
An **anchor** is the pair (sequence, hash) of the chain head written to an immutable blob (version-level immutability,
write-once). Verification then also checks that the database still contains the record the latest anchor names, with the
same hash: rewriting history or dropping the tail contradicts something the database owner cannot change.

* Anchors are grouped in **epochs**. A new epoch begins only through an explicit, audited operator action after a
  restore (``orqentis audit start-epoch``; docs/operations/restore.md); the anchors of the previous epoch stay in the
  immutable container as the evidence of what the restore lost.
* ``put`` is write-once and idempotent: anchoring the same (epoch, sequence, hash) again is a no-op, and a *different*
  hash for an already anchored sequence is kept as a second anchor so that ``latest`` can report the conflict.
* The anchors are re-derived from nothing the application can edit afterwards, so the application deletes none.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pydantic import AwareDatetime, BaseModel, ConfigDict

from orqentis.config.clock import Clock
from orqentis.observability.logging import get_logger

_log = get_logger(__name__)

_NAME = re.compile(r"^epoch-(\d{4})/(\d{12})-([0-9a-f]{16})\.json$")


class AnchorConflictError(Exception):
    """Two different hashes are anchored for the same sequence: the chain was rewritten, or two chains were mixed."""


class AuditAnchor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    epoch: int
    sequence: int
    hash: str
    anchored_at: AwareDatetime

    @property
    def name(self) -> str:
        """Object name; lexicographic order is numeric order, and the hash prefix keeps conflicting anchors apart."""
        return f"epoch-{self.epoch:04d}/{self.sequence:012d}-{self.hash[:16]}.json"


class AuditAnchorStore(Protocol):
    async def put(self, anchor: AuditAnchor) -> bool:
        """Store ``anchor`` write-once. ``False`` when exactly this anchor already existed."""
        ...

    async def latest(self, epoch: int) -> AuditAnchor | None:
        """The anchor with the highest sequence in ``epoch``; raises :class:`AnchorConflictError` on disagreement."""
        ...


@dataclass(frozen=True)
class ChainHead:
    epoch: int
    sequence: int
    hash: str


class AnchorableAudit(Protocol):
    async def head(self) -> ChainHead: ...


def pick_latest(anchors: list[AuditAnchor]) -> AuditAnchor | None:
    """The highest-sequence anchor, or a conflict if two different hashes share that sequence."""
    if not anchors:
        return None
    top = max(a.sequence for a in anchors)
    at_top = {a.hash for a in anchors if a.sequence == top}
    if len(at_top) > 1:
        raise AnchorConflictError(f"Conflicting audit anchors for sequence {top}: {sorted(h[:12] for h in at_top)}")
    return next(a for a in anchors if a.sequence == top)


def parse_anchor_name(name: str) -> tuple[int, int, str] | None:
    """(epoch, sequence, hash prefix) from an object name, or None when it is not an anchor."""
    match = _NAME.match(name)
    return (int(match.group(1)), int(match.group(2)), match.group(3)) if match else None


class LocalAuditAnchorStore:
    """Anchors as files under a directory (development and tests; a directory is not immutable)."""

    def __init__(self, root: Path) -> None:
        self._root = root

    async def put(self, anchor: AuditAnchor) -> bool:
        return await asyncio.to_thread(self._put, anchor)

    def _put(self, anchor: AuditAnchor) -> bool:
        path = self._root / anchor.name
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("x", encoding="utf-8") as fh:
                fh.write(anchor.model_dump_json())
        except FileExistsError:
            return False
        return True

    async def latest(self, epoch: int) -> AuditAnchor | None:
        return await asyncio.to_thread(self._latest, epoch)

    def _latest(self, epoch: int) -> AuditAnchor | None:
        folder = self._root / f"epoch-{epoch:04d}"
        if not folder.is_dir():
            return None
        anchors = [
            AuditAnchor.model_validate_json(p.read_text(encoding="utf-8"))
            for p in sorted(folder.iterdir())
            if parse_anchor_name(f"{folder.name}/{p.name}") is not None
        ]
        return pick_latest(anchors)


class AuditAnchorTask:
    """Scheduled job: anchor the chain head when it moved since the last anchor."""

    def __init__(self, audit: AnchorableAudit, store: AuditAnchorStore, clock: Clock) -> None:
        self._audit = audit
        self._store = store
        self._clock = clock
        self._last: tuple[int, int] | None = None  # (epoch, sequence) already anchored by this process

    async def run(self) -> AuditAnchor | None:
        head = await self._audit.head()
        if head.sequence == 0 or self._last == (head.epoch, head.sequence):
            return None
        anchor = AuditAnchor(epoch=head.epoch, sequence=head.sequence, hash=head.hash, anchored_at=self._clock.now())
        created = await self._store.put(anchor)
        self._last = (head.epoch, head.sequence)
        if created:
            _log.info("audit head anchored", extra={"epoch": head.epoch, "sequence": head.sequence})
        return anchor if created else None
