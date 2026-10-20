"""The local audit log when a second process (the other revision during a deploy) appends to the same file."""

from __future__ import annotations

from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path

import pytest

from orqentis.config.clock import FixedClock
from orqentis.observability.audit import LocalAuditLog

CLOCK = FixedClock(datetime(2026, 10, 20, 8, 0, tzinfo=UTC))


async def test_interleaved_appends_from_two_writers_chain_in_one_sequence(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    old, new = LocalAuditLog(path, CLOCK), LocalAuditLog(path, CLOCK)
    for writer in (old, new, new, old, new, old):
        await writer.record(actor="x", action="a", outcome="OK")
    records = await old.list()
    assert [r.sequence for r in records] == [1, 2, 3, 4, 5, 6]
    for previous, record in pairwise(records):
        assert record.prev_hash == previous.hash
    assert (await new.verify()).valid


async def test_a_writer_that_is_alone_does_not_reread_the_file_on_every_append(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = LocalAuditLog(tmp_path / "audit.jsonl", CLOCK)
    reads = 0
    original = log._load_tail

    def counting() -> tuple[str, int]:
        nonlocal reads
        reads += 1
        return original()

    monkeypatch.setattr(log, "_load_tail", counting)
    for _ in range(5):
        await log.record(actor="x", action="a", outcome="OK")
    assert reads == 0  # the size it left behind is what it finds, so there is nothing to re-read


async def test_the_chain_head_is_found_even_when_the_last_record_is_larger_than_the_read_window(
    tmp_path: Path,
) -> None:
    path = tmp_path / "audit.jsonl"
    writer = LocalAuditLog(path, CLOCK)
    await writer.record(actor="x", action="small", outcome="OK")
    await writer.record(actor="x", action="huge", outcome="OK", details={"note": "y" * 300_000})

    reopened = LocalAuditLog(path, CLOCK)  # finds the head by reading the end of the file, not the whole log
    record = await reopened.record(actor="x", action="after", outcome="OK")
    assert record.sequence == 3
    assert (await reopened.verify()).valid


async def test_a_half_written_last_line_does_not_stop_a_new_process_from_starting(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    writer = LocalAuditLog(path, CLOCK)
    complete = await writer.record(actor="x", action="a", outcome="OK")
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"sequence": 2, "audit_id": "AUD-half')  # the other process is in the middle of a write

    started = LocalAuditLog(path, CLOCK)  # falls back to the last complete record rather than crashing
    assert (started._sequence, started._last_hash) == (1, complete.hash)
