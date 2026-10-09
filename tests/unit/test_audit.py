"""Tamper-evident audit trail."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from orqentis.config.clock import FixedClock
from orqentis.observability.audit import GENESIS_HASH, LocalAuditLog

CLOCK = FixedClock(datetime(2026, 10, 5, tzinfo=UTC))


async def test_hash_chain_verifies_and_survives_restart(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    log = LocalAuditLog(path, CLOCK)
    first = await log.record(actor="a", action="x", outcome="OK", details={"client_secret": "s"})
    assert first.prev_hash == GENESIS_HASH
    assert first.details["client_secret"] == "[REDACTED]"
    reopened = LocalAuditLog(path, CLOCK)
    second = await reopened.record(actor="b", action="y", outcome="OK", incident_id="INC-1")
    assert second.sequence == 2 and second.prev_hash == first.hash
    assert (await reopened.verify()).valid
    assert [r.actor for r in await reopened.list(incident_id="INC-1")] == ["b"]


async def test_tampering_is_detected(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    log = LocalAuditLog(path, CLOCK)
    for i in range(3):
        await log.record(actor="a", action="tool.invoke", outcome="SUCCESS", details={"i": i})
    lines = path.read_text().splitlines()
    record = json.loads(lines[1])
    record["outcome"] = "DENIED"
    lines[1] = json.dumps(record)
    path.write_text("\n".join(lines) + "\n")
    result = await LocalAuditLog(path, CLOCK).verify()
    assert result.valid is False
    assert result.first_invalid_sequence == 2


async def test_deleted_record_is_detected(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    log = LocalAuditLog(path, CLOCK)
    for i in range(3):
        await log.record(actor="a", action="x", outcome="OK", details={"i": i})
    lines = path.read_text().splitlines()
    path.write_text("\n".join([lines[0], lines[2]]) + "\n")
    result = await LocalAuditLog(path, CLOCK).verify()
    assert result.valid is False and result.reason == "sequence gap"
