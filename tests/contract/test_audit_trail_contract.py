"""AuditTrail contract: a hash chain with contiguous sequences, redacted details, and an honest verification."""

from __future__ import annotations

import asyncio
from itertools import pairwise

from orqentis.observability.audit import GENESIS_HASH, AuditTrail


async def test_records_form_a_chain_from_the_genesis_hash(audit_trail: AuditTrail) -> None:
    assert (await audit_trail.verify()).valid
    first = await audit_trail.record(actor="a", action="x", outcome="OK")
    second = await audit_trail.record(actor="b", action="y", outcome="FAILED", incident_id="INC-1", trace_id="t-1")
    assert (first.sequence, second.sequence) == (1, 2)
    assert first.prev_hash == GENESIS_HASH and second.prev_hash == first.hash
    assert first.hash != second.hash and len(first.hash) == 64
    result = await audit_trail.verify()
    assert result.valid and result.records == 2


async def test_details_are_redacted_before_they_are_chained(audit_trail: AuditTrail) -> None:
    record = await audit_trail.record(
        actor="a", action="x", outcome="OK", details={"password": "hunter2", "note": "Bearer abc.def.ghi", "n": 3}
    )
    assert record.details["password"] == "[REDACTED]" and "abc.def" not in str(record.details)
    assert record.details["n"] == 3
    assert (await audit_trail.list())[0] == record


async def test_list_filters_by_incident_and_returns_the_newest_when_limited(audit_trail: AuditTrail) -> None:
    for i in range(6):
        await audit_trail.record(actor="a", action=f"step-{i}", outcome="OK", incident_id="INC-1" if i % 2 else None)
    everything = await audit_trail.list()
    assert [r.sequence for r in everything] == [1, 2, 3, 4, 5, 6]
    assert [r.action for r in await audit_trail.list(incident_id="INC-1")] == ["step-1", "step-3", "step-5"]
    assert [r.sequence for r in await audit_trail.list(limit=2)] == [5, 6]  # the newest two, oldest first
    assert [r.sequence for r in await audit_trail.list(incident_id="INC-1", limit=1)] == [6]
    assert await audit_trail.list(incident_id="INC-404") == []


async def test_records_round_trip_through_storage_without_changing_their_hash(audit_trail: AuditTrail) -> None:
    written = await audit_trail.record(
        actor="é", action="x", outcome="OK", details={"nested": {"a": [1, 2.5, None, True]}, "t": "line\nbreak"}
    )
    (read,) = await audit_trail.list()
    assert read == written
    assert (await audit_trail.verify()).valid


async def test_concurrent_appends_produce_one_contiguous_chain(audit_trail: AuditTrail) -> None:
    records = await asyncio.gather(
        *(audit_trail.record(actor=f"w{n}", action="burst", outcome="OK") for n in range(30))
    )
    assert sorted(r.sequence for r in records) == list(range(1, 31))
    chain = await audit_trail.list()
    assert [r.sequence for r in chain] == list(range(1, 31))
    for before, after in pairwise(chain):
        assert after.prev_hash == before.hash
    assert (await audit_trail.verify()).valid
