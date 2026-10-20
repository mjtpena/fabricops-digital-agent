"""The local audit log when a second process (the other revision during a deploy) appends to the same file."""

from __future__ import annotations

from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path

import httpx
import pytest

from orqentis.api.app import create_app
from orqentis.bootstrap import Container
from orqentis.config.clock import FixedClock
from orqentis.observability.audit import AuditIntegrityError, LocalAuditLog

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


# ------------------------------------------------------------------------------------ review round 2: shrinking
async def test_truncating_the_tail_of_a_live_log_is_refused_not_absorbed(tmp_path: Path) -> None:
    """Another writer only ever makes the file bigger; a smaller file means records were removed."""
    path = tmp_path / "audit.jsonl"
    log = LocalAuditLog(path, CLOCK)
    for _ in range(5):
        await log.record(actor="x", action="a", outcome="OK")
    kept = path.read_text(encoding="utf-8").splitlines(keepends=True)[:3]
    path.write_text("".join(kept), encoding="utf-8")  # the last two records are cut off while the app runs

    with pytest.raises(AuditIntegrityError, match="shrank"):
        await log.record(actor="x", action="after", outcome="OK")
    assert path.read_text(encoding="utf-8").splitlines(keepends=True) == kept  # nothing was appended or re-chained

    verification = await log.verify()
    assert not verification.valid and verification.records == 3
    assert verification.first_invalid_sequence == 4 and "truncated" in (verification.reason or "")
    with pytest.raises(AuditIntegrityError):  # and it stays refused: a breach is not forgotten
        await log.record(actor="x", action="again", outcome="OK")


async def test_a_breach_is_remembered_even_if_the_file_grows_back(tmp_path: Path) -> None:
    """Restoring or padding the file afterwards must not quietly re-enable appends."""
    path = tmp_path / "audit.jsonl"
    log = LocalAuditLog(path, CLOCK)
    for _ in range(3):
        await log.record(actor="x", action="a", outcome="OK")
    original = path.read_text(encoding="utf-8")
    path.write_text("".join(original.splitlines(keepends=True)[:1]), encoding="utf-8")
    with pytest.raises(AuditIntegrityError):
        await log.record(actor="x", action="after-truncation", outcome="OK")

    path.write_text(original + original, encoding="utf-8")  # now bigger than the process ever saw it
    with pytest.raises(AuditIntegrityError, match="shrank"):
        await log.record(actor="x", action="after-growth", outcome="OK")
    assert not (await log.verify()).valid


async def test_a_replaced_or_emptied_log_is_a_breach_too(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    log = LocalAuditLog(path, CLOCK)
    await log.record(actor="x", action="a", outcome="OK")
    path.write_text("", encoding="utf-8")
    with pytest.raises(AuditIntegrityError):
        await log.record(actor="x", action="b", outcome="OK")
    assert not (await log.verify()).valid


async def test_verification_notices_truncation_even_if_nothing_was_appended_since(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    log = LocalAuditLog(path, CLOCK)
    for _ in range(4):
        await log.record(actor="x", action="a", outcome="OK")
    path.write_text("".join(path.read_text(encoding="utf-8").splitlines(keepends=True)[:2]), encoding="utf-8")
    verification = await log.verify()  # the chain that is left is internally fine; the process knows it wrote 4
    assert not verification.valid and "truncated" in (verification.reason or "")


async def test_a_deletion_in_the_middle_keeps_its_original_diagnosis(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    log = LocalAuditLog(path, CLOCK)
    for _ in range(3):
        await log.record(actor="x", action="a", outcome="OK")
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    path.write_text(lines[0] + lines[2], encoding="utf-8")
    verification = await log.verify()
    assert not verification.valid and verification.reason == "sequence gap"


async def test_a_failed_write_does_not_consume_a_sequence_number(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = LocalAuditLog(tmp_path / "audit.jsonl", CLOCK)
    await log.record(actor="x", action="a", outcome="OK")
    original = log._append

    def disk_full(line: str) -> int:
        raise OSError("no space left on device")

    monkeypatch.setattr(log, "_append", disk_full)
    with pytest.raises(OSError, match="no space"):
        await log.record(actor="x", action="b", outcome="OK")
    monkeypatch.setattr(log, "_append", original)
    record = await log.record(actor="x", action="c", outcome="OK")
    assert record.sequence == 2
    assert (await log.verify()).valid


async def test_the_audit_endpoint_reports_a_truncated_live_log(container: Container) -> None:
    await container.audit.record(actor="x", action="a", outcome="OK")
    await container.audit.record(actor="x", action="b", outcome="OK")
    path = container.settings.audit_log_path
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    path.write_text("".join(lines[:-2]), encoding="utf-8")  # the app is running; the last two records vanish

    app = create_app(container=container)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http,
    ):
        body = (await http.get("/audit/verify", headers={"X-Orqentis-User": "victor@contoso.com"})).json()
    assert body["valid"] is False and "truncated" in body["reason"]
    with pytest.raises(AuditIntegrityError):
        await container.audit.record(actor="x", action="after", outcome="OK")
