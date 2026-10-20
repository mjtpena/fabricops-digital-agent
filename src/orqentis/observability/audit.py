"""Tamper-evident audit trail (hash-chained, append-only JSON lines)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from pydantic import AwareDatetime, BaseModel, Field

from orqentis.config.clock import Clock
from orqentis.domain.common import new_id
from orqentis.observability.redaction import redact

GENESIS_HASH = "0" * 64


class AuditRecord(BaseModel):
    sequence: int
    audit_id: str
    timestamp: AwareDatetime
    actor: str
    action: str
    outcome: str
    incident_id: str | None = None
    trace_id: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    prev_hash: str
    hash: str


class AuditVerification(BaseModel):
    valid: bool
    records: int
    first_invalid_sequence: int | None = None
    reason: str | None = None


class AuditTrail(Protocol):
    async def record(
        self,
        *,
        actor: str,
        action: str,
        outcome: str,
        incident_id: str | None = None,
        trace_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> AuditRecord: ...

    async def list(self, *, incident_id: str | None = None, limit: int | None = None) -> list[AuditRecord]: ...

    async def verify(self) -> AuditVerification: ...


def _digest(prev_hash: str, body: dict[str, Any]) -> str:
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(f"{prev_hash}|{canonical}".encode()).hexdigest()


def _body(record: AuditRecord) -> dict[str, Any]:
    return record.model_dump(mode="json", exclude={"hash"})


class LocalAuditLog:
    """JSONL file where each record carries the SHA-256 of its predecessor.

    A single process owns the chain. During a deploy, though, Container Apps can run the old and the new revision
    against the same volume for a while, so before each append the log checks whether the file grew since *this*
    process last wrote and, if so, re-reads the chain head (last hash and sequence). That makes interleaved appends
    from two processes chain instead of fork. It is best effort: SMB offers no file locks, so two appends in the very
    same instant can still collide. Real multi-writer safety needs the database-serialised chain (PostgreSQL).
    """

    def __init__(self, path: Path, clock: Clock) -> None:
        self._path = path
        self._clock = clock
        self._lock = asyncio.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._last_hash, self._sequence = self._load_tail()
        self._size = self._file_size()

    def _file_size(self) -> int:
        return self._path.stat().st_size if self._path.exists() else 0

    def _load_tail(self) -> tuple[str, int]:
        """Hash and sequence of the last record, reading only the end of the file."""
        if not self._path.exists():
            return GENESIS_HASH, 0
        with self._path.open("rb") as fh:
            size = fh.seek(0, os.SEEK_END)
            window = 64 * 1024
            while True:
                start = max(0, size - window)
                fh.seek(start)
                lines = [line for line in fh.read(size - start).splitlines() if line.strip()]
                # With ``start > 0`` the first line may be cut off: it is only usable when it is not the only one.
                if start == 0 or len(lines) >= 2:
                    break
                window *= 4
        # The newest line may be half written by another process right now; fall back to the one before it.
        for line in reversed(lines):
            try:
                last = AuditRecord.model_validate_json(line)
            except ValueError:
                continue
            return last.hash, last.sequence
        return GENESIS_HASH, 0

    def _sync_tail(self) -> None:
        if self._file_size() != self._size:  # somebody else appended since we last wrote
            self._last_hash, self._sequence = self._load_tail()
            self._size = self._file_size()

    async def record(
        self,
        *,
        actor: str,
        action: str,
        outcome: str,
        incident_id: str | None = None,
        trace_id: str | None = None,
        details: dict[str, Any] | None = None,
        timestamp: datetime | None = None,
    ) -> AuditRecord:
        async with self._lock:
            await asyncio.to_thread(self._sync_tail)
            self._sequence += 1
            draft = AuditRecord(
                sequence=self._sequence,
                audit_id=new_id("AUD"),
                timestamp=timestamp or self._clock.now(),
                actor=actor,
                action=action,
                outcome=outcome,
                incident_id=incident_id,
                trace_id=trace_id,
                details=redact(details or {}),
                prev_hash=self._last_hash,
                hash="",
            )
            record = draft.model_copy(update={"hash": _digest(self._last_hash, _body(draft))})
            line = record.model_dump_json() + "\n"
            self._size = await asyncio.to_thread(self._append, line)
            self._last_hash = record.hash
            return record

    def _append(self, line: str) -> int:
        """Append one record and return the size of the file afterwards."""
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
            return os.fstat(fh.fileno()).st_size

    def _read_all(self) -> list[AuditRecord]:
        if not self._path.exists():
            return []
        with self._path.open(encoding="utf-8") as fh:
            return [AuditRecord.model_validate_json(line) for line in fh if line.strip()]

    async def list(self, *, incident_id: str | None = None, limit: int | None = None) -> list[AuditRecord]:
        records = await asyncio.to_thread(self._read_all)
        if incident_id is not None:
            records = [r for r in records if r.incident_id == incident_id]
        return records[-limit:] if limit else records

    async def verify(self) -> AuditVerification:
        records = await asyncio.to_thread(self._read_all)
        prev = GENESIS_HASH
        for expected_seq, record in enumerate(records, start=1):
            if record.sequence != expected_seq:
                return AuditVerification(
                    valid=False,
                    records=len(records),
                    first_invalid_sequence=record.sequence,
                    reason="sequence gap",
                )
            if record.prev_hash != prev or _digest(prev, _body(record)) != record.hash:
                return AuditVerification(
                    valid=False,
                    records=len(records),
                    first_invalid_sequence=record.sequence,
                    reason="hash chain broken",
                )
            prev = record.hash
        return AuditVerification(valid=True, records=len(records))
