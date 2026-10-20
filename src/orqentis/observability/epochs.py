"""Audit epoch markers: the one thing about the audit chain that the application cannot write.

The chain head lives in the database, and so does its **epoch** counter (see :mod:`orqentis.observability.anchors`).
Whoever can write the database can therefore rewrite the whole chain and also move the counter, so that the rewritten
chain is judged against an epoch nobody anchored. An **epoch marker** closes that: opening a new epoch (only ever done
by an operator after a restore, ``orqentis audit start-epoch``) writes the marker for it to a container where the
application's identity has *read* access only, and verification demands that the database is in the highest epoch that
has a marker. Neither a database-only attacker nor a compromised application can open an epoch.

A marker names the ``audit.epoch`` record that opened its epoch (sequence and hash), who opened it and why. Markers
are write-once. Epoch 1 has no marker: it begins at the genesis of the chain.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

_NAME = re.compile(r"^epoch-(\d{4})\.json$")
_HASH = r"^[0-9a-f]{64}$"


class EpochMarkerAccessError(PermissionError):
    """The identity may read epoch markers but not write them (it is not in the operator group)."""


class EpochMarkerConflictError(Exception):
    """A different marker already exists for this epoch (an earlier attempt that did not complete, or a collision)."""


class EpochMarker(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    epoch: int = Field(ge=2)
    sequence: int = Field(ge=1)  # the audit.epoch record that opened the epoch
    hash: str = Field(pattern=_HASH)
    started_at: AwareDatetime
    started_by: str = Field(max_length=200)
    reason: str = Field(max_length=500)

    @property
    def name(self) -> str:
        return f"epoch-{self.epoch:04d}.json"


def parse_marker_name(name: str) -> int | None:
    match = _NAME.match(name)
    return int(match.group(1)) if match else None


class EpochMarkerStore(Protocol):
    async def put(self, marker: EpochMarker) -> bool:
        """Store ``marker`` write-once. ``False`` when exactly this marker already existed.

        Raises :class:`EpochMarkerConflictError` when another marker exists for the epoch. Needs write access: the
        application's identity does not have it.
        """
        ...

    async def all(self) -> Sequence[EpochMarker]:
        """Every marker, lowest epoch first."""
        ...


def current_epoch(markers: Sequence[EpochMarker]) -> int:
    """The epoch the database must be in: the highest with a marker, or 1 when none was ever opened."""
    return max((m.epoch for m in markers), default=1)


class LocalEpochMarkerStore:
    """Markers as files under a directory (development and tests; a directory is not access-controlled)."""

    def __init__(self, root: Path) -> None:
        self._root = root

    async def put(self, marker: EpochMarker) -> bool:
        return await asyncio.to_thread(self._put, marker)

    def _put(self, marker: EpochMarker) -> bool:
        path = self._root / marker.name
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("x", encoding="utf-8") as fh:
                fh.write(marker.model_dump_json())
        except FileExistsError:
            existing = EpochMarker.model_validate_json(path.read_text(encoding="utf-8"))
            if existing.sequence == marker.sequence and existing.hash == marker.hash:
                return False
            raise EpochMarkerConflictError(f"a different marker exists for audit epoch {marker.epoch}") from None
        return True

    async def all(self) -> Sequence[EpochMarker]:
        return await asyncio.to_thread(self._all)

    def _all(self) -> Sequence[EpochMarker]:
        if not self._root.is_dir():
            return []
        found = [
            EpochMarker.model_validate_json(p.read_text(encoding="utf-8"))
            for p in sorted(self._root.iterdir())
            if parse_marker_name(p.name) is not None
        ]
        return sorted(found, key=lambda m: m.epoch)
