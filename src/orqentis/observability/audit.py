"""Tamper-evident audit trail (hash-chained, append-only JSON lines)."""

from __future__ import annotations

import asyncio
import hashlib
import json
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
    """JSONL file where each record carries the SHA-256 of its predecessor."""

    def __init__(self, path: Path, clock: Clock) -> None:
        self._path = path
        self._clock = clock
        self._lock = asyncio.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._last_hash, self._sequence = self._load_tail()

    def _load_tail(self) -> tuple[str, int]:
        if not self._path.exists():
            return GENESIS_HASH, 0
        last: AuditRecord | None = None
        with self._path.open(encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    last = AuditRecord.model_validate_json(line)
        return (last.hash, last.sequence) if last else (GENESIS_HASH, 0)

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
            await asyncio.to_thread(self._append, line)
            self._last_hash = record.hash
            return record

    def _append(self, line: str) -> None:
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(line)

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
