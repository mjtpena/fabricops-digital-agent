"""Audit anchors: the head of the hash chain, copied periodically to storage the database cannot rewrite.

A hash chain proves that records were not *altered* without the break being visible, but whoever can rewrite the whole
table can recompute every hash, and whoever can delete the tail leaves a shorter chain that is still internally valid.
An **anchor** is the pair (sequence, hash) of the chain head written to an immutable blob (version-level immutability,
write-once). Verification checks **every** anchor: at each anchored sequence the database must still contain the
record with the anchored hash. Rewriting history, dropping the tail, or dropping the tail and growing a different
chain past it each contradict an anchor the database owner cannot change. (Checking only the latest anchor is not
enough: whoever rewrote the chain could simply wait for the next anchor to cover the rewritten head. Old anchors are
what stop that.)

* An anchor's object name carries the **whole hash**: ``epoch-NNNN/<sequence, 12 digits>-<64 hex>.json``. Listing the
  container is therefore enough to verify every anchor ever written, without reading each blob.
* Anchors are grouped in **epochs**. A new epoch begins only through an explicit operator action after a restore
  (``orqentis audit start-epoch``; docs/operations/restore.md), which writes an *epoch marker*
  (:mod:`orqentis.observability.epochs`) that the application's identity cannot write. The database must be in the epoch
  of the highest marker. Anchors of an earlier epoch that lie at or beyond the sequence where the next epoch opened
  describe records the restore lost; they stay in the container as evidence and are not held against the new chain.
* ``put`` is write-once and idempotent: anchoring the same (epoch, sequence, hash) again is a no-op, and a *different*
  hash for an already anchored sequence is kept as a second anchor so that verification reports the conflict.
* The application never deletes an anchor, and the container does not let anyone before the retention ends.
* **The window.** Tampering is detectable up to the last anchor. Records newer than it are protected by the hash chain
  alone: someone with full database rights could rewrite that last interval before the next anchor covers it. The
  interval (``ORQENTIS_SCHEDULER_AUDIT_ANCHOR_INTERVAL_MINUTES``) bounds that window; it cannot be zero.

The :class:`AuditAnchorTask` also refuses to anchor a chain that contradicts what is already anchored (so a tampered
head never enters the immutable record, where it would be permanent), and says so loudly.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Collection, Sequence
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Protocol

from pydantic import AwareDatetime, BaseModel, ConfigDict

from orqentis.config.clock import Clock
from orqentis.observability.epochs import EpochMarker, EpochMarkerStore, current_epoch
from orqentis.observability.logging import get_logger

_log = get_logger(__name__)

_NAME = re.compile(r"^epoch-(\d{4})/(\d{12})-([0-9a-f]{64})\.json$")

# The scheduled audit verification (WS9) alerts on this log message; the anchor task uses the same words for the same
# condition, so one alert covers "the audit chain does not match what was anchored".
AUDIT_VERIFICATION_FAILURE_MESSAGE = "audit chain verification failed"


class AnchorConflictError(Exception):
    """Two different hashes are anchored for the same sequence: the chain was rewritten, or two chains were mixed."""


class AnchorRefusedError(Exception):
    """The anchor task did not anchor the head: it contradicts what is already anchored. Needs a person."""


@dataclass(frozen=True)
class AnchorRef:
    """An anchor as its object name gives it: enough to check it against the chain."""

    epoch: int
    sequence: int
    hash: str


class AuditAnchor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    epoch: int
    sequence: int
    hash: str
    anchored_at: AwareDatetime

    @property
    def name(self) -> str:
        """Object name; lexicographic order is numeric order, and the full hash keeps conflicting anchors apart."""
        return f"epoch-{self.epoch:04d}/{self.sequence:012d}-{self.hash}.json"

    @property
    def ref(self) -> AnchorRef:
        return AnchorRef(self.epoch, self.sequence, self.hash)


def anchor_name(ref: AnchorRef) -> str:
    return f"epoch-{ref.epoch:04d}/{ref.sequence:012d}-{ref.hash}.json"


class AuditAnchorStore(Protocol):
    async def put(self, anchor: AuditAnchor) -> bool:
        """Store ``anchor`` write-once. ``False`` when exactly this anchor already existed."""
        ...

    async def latest(self, epoch: int, *, exclude: Collection[str] = ()) -> AuditAnchor | None:
        """The anchor with the highest sequence in ``epoch``; raises :class:`AnchorConflictError` on disagreement.

        ``exclude`` are object names an operator has disregarded (see ``EpochMarker.disregarded_anchors``).
        """
        ...

    async def refs(self, epoch: int) -> list[AnchorRef]:
        """Every anchor of ``epoch`` by name (lowest sequence first), without reading the objects."""
        ...


@dataclass(frozen=True)
class ChainHead:
    epoch: int
    sequence: int
    hash: str


class AnchorableAudit(Protocol):
    async def head(self) -> ChainHead: ...

    async def record_hash_at(self, sequence: int) -> str | None:
        """The hash of the stored record at ``sequence``, or ``None`` when there is none."""
        ...


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
    """(epoch, sequence, hash) from an object name, or None when it is not an anchor."""
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

    async def refs(self, epoch: int) -> list[AnchorRef]:
        return await asyncio.to_thread(self._refs, epoch)

    def _refs(self, epoch: int) -> list[AnchorRef]:
        folder = self._root / f"epoch-{epoch:04d}"
        if not folder.is_dir():
            return []
        parsed = (parse_anchor_name(f"{folder.name}/{p.name}") for p in sorted(folder.iterdir()))
        return [AnchorRef(e, s, h) for e, s, h in (x for x in parsed if x is not None)]

    async def latest(self, epoch: int, *, exclude: Collection[str] = ()) -> AuditAnchor | None:
        return await asyncio.to_thread(self._latest, epoch, frozenset(exclude))

    def _latest(self, epoch: int, exclude: frozenset[str]) -> AuditAnchor | None:
        folder = self._root / f"epoch-{epoch:04d}"
        if not folder.is_dir():
            return None
        anchors = [
            AuditAnchor.model_validate_json(p.read_text(encoding="utf-8"))
            for p in sorted(folder.iterdir())
            if parse_anchor_name(f"{folder.name}/{p.name}") is not None and f"{folder.name}/{p.name}" not in exclude
        ]
        return pick_latest(anchors)


class AuditAnchorTask:
    """Scheduled job: anchor the chain head when it moved since the last anchor, and only if it is the same chain.

    Before anchoring it checks, with a handful of reads (not a full verification), that the head does not contradict
    what is already anchored:

    * the database is in the epoch of the highest epoch marker (when a marker store is configured);
    * the head is not behind the previous anchor of this epoch, and the record at that anchor's sequence still has the
      anchored hash;
    * for an epoch above 1, the record that opened it still has the hash the marker names.

    When any of these fails it anchors nothing, logs an ERROR with the message the audit verification alert watches
    (:data:`AUDIT_VERIFICATION_FAILURE_MESSAGE`), calls ``on_failure`` and raises :class:`AnchorRefusedError` (the
    scheduler then reports the job as failed). It keeps refusing on every run until the chain is consistent with its
    anchors again or an operator has opened a new epoch.
    """

    def __init__(
        self,
        audit: AnchorableAudit,
        store: AuditAnchorStore,
        clock: Clock,
        *,
        epochs: EpochMarkerStore | None = None,
        on_failure: Callable[[str], Awaitable[None] | None] | None = None,
        stale_after: timedelta | None = None,
    ) -> None:
        self._audit = audit
        self._store = store
        self._clock = clock
        self._epochs = epochs
        self._on_failure = on_failure
        self._stale_after = stale_after
        self._last: tuple[int, int] | None = None  # (epoch, sequence) already anchored by this process
        self._last_ok = clock.now()  # the last run that anchored, or found nothing new to anchor, without a problem

    @property
    def stale(self) -> bool:
        """No run has succeeded for longer than ``stale_after`` (twice the anchor interval): anchoring is broken."""
        return self._stale_after is not None and self._clock.now() - self._last_ok > self._stale_after

    async def run(self) -> AuditAnchor | None:
        # Read in this order: the epoch, then the anchors, then the head. An anchor written by another replica in
        # between (a leader handover) can then never be ahead of the head we compare it with.
        epoch = (await self._audit.head()).epoch
        markers = list(await self._epochs.all()) if self._epochs is not None else []
        previous: AuditAnchor | None = None
        conflict: str | None = None
        try:
            previous = await self._store.latest(epoch)
        except AnchorConflictError as exc:
            conflict = str(exc)
        head = await self._audit.head()
        if head.epoch != epoch:  # an epoch was opened in between: look again on the next run
            return None
        if head.sequence == 0:
            self._last_ok = self._clock.now()
            return None
        # Checked on every run, also when the head did not move: a chain that stops matching its anchors should be
        # reported at the next interval, not only when someone appends to it.
        if (problem := await self._continuity_problem(head, markers, previous, conflict)) is not None:
            _log.error(
                AUDIT_VERIFICATION_FAILURE_MESSAGE,
                extra={
                    "check": "anchor-continuity",
                    "result": "invalid",
                    "epoch": head.epoch,
                    "head_sequence": head.sequence,
                    "reason": problem,
                },
            )
            if self._on_failure is not None:
                outcome = self._on_failure(problem)
                if outcome is not None:
                    await outcome
            raise AnchorRefusedError(problem)
        if self._last == (head.epoch, head.sequence):
            self._last_ok = self._clock.now()
            return None
        anchor = AuditAnchor(epoch=head.epoch, sequence=head.sequence, hash=head.hash, anchored_at=self._clock.now())
        created = await self._store.put(anchor)
        self._last = (head.epoch, head.sequence)
        self._last_ok = self._clock.now()
        if created:
            _log.info("audit head anchored", extra={"epoch": head.epoch, "sequence": head.sequence})
        return anchor if created else None

    async def _continuity_problem(
        self, head: ChainHead, markers: Sequence[EpochMarker], previous: AuditAnchor | None, conflict: str | None
    ) -> str | None:
        marker = None
        if self._epochs is not None:
            expected = current_epoch(markers)
            if head.epoch != expected:
                return (
                    f"the database is in audit epoch {head.epoch} but the epoch markers say {expected}: the epoch "
                    "counter was moved, or the database was restored, without `orqentis audit start-epoch`"
                )
            marker = next((m for m in markers if m.epoch == head.epoch), None)
        if conflict is not None:
            return conflict
        if previous is not None:
            if head.sequence < previous.sequence:
                return (
                    f"the chain head (sequence {head.sequence}) is behind the latest anchor of its epoch "
                    f"(sequence {previous.sequence}): records were lost, for example by a restore"
                )
            if await self._audit.record_hash_at(previous.sequence) != previous.hash:
                return (
                    f"the record at sequence {previous.sequence} is not the one that was anchored: the chain was "
                    "rewritten, or restored and grown past its anchor"
                )
        if marker is not None and await self._audit.record_hash_at(marker.sequence) != marker.hash:
            return f"the record that opened audit epoch {head.epoch} is not the one its marker names"
        return None
