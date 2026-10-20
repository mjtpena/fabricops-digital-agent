"""One valid audit chain from any number of writers, and a verification that notices tampering."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from itertools import pairwise

import asyncpg
import pytest
from tests.support.services import PostgresServer

from orqentis.config.clock import FixedClock, SystemClock
from orqentis.observability.anchors import AuditAnchor, AuditAnchorTask, LocalAuditAnchorStore
from orqentis.observability.audit import GENESIS_HASH, AuditRecord, record_digest
from orqentis.storage.postgres import PostgresAuditTrail, PostgresConfig, PostgresStorage


async def second_instance(config: PostgresConfig) -> PostgresStorage:
    return await PostgresStorage.open(config, None, SystemClock(), migrate=False)


async def test_two_instances_appending_concurrently_produce_one_valid_chain(
    pg: PostgresStorage, pg_config: PostgresConfig
) -> None:
    other = await second_instance(pg_config)
    try:
        writers = [pg.audit, other.audit]
        records = await asyncio.gather(
            *(
                writers[n % 2].record(actor=f"instance-{n % 2}", action="load", outcome="OK", details={"n": n})
                for n in range(120)
            )
        )
        assert sorted(r.sequence for r in records) == list(range(1, 121))  # no number twice, none missing
        chain = await pg.audit.list()
        assert [r.sequence for r in chain] == list(range(1, 121))
        assert chain[0].prev_hash == GENESIS_HASH
        for before, after in pairwise(chain):
            assert after.prev_hash == before.hash  # a single line of descent: the chain never forked
        for first, second in ((pg.audit, other.audit), (other.audit, pg.audit)):
            verification = await first.verify()
            assert verification.valid and verification.records == 120
            assert (await second.head()).sequence == 120
        assert {r.actor for r in chain} == {"instance-0", "instance-1"}
    finally:
        await other.close()


async def test_a_failed_append_consumes_no_sequence_number_and_forks_nothing(pg: PostgresStorage) -> None:
    await pg.audit.record(actor="a", action="one", outcome="OK")
    with pytest.raises(Exception, match="serialize"):  # the transaction holds the head lock when this raises
        await pg.audit.record(actor="a", action="bad", outcome="OK", details={"unserialisable": object()})
    after = await pg.audit.record(actor="a", action="two", outcome="OK")
    assert after.sequence == 2
    assert (await pg.audit.verify()).valid and (await pg.audit.head()).sequence == 2


async def test_a_writer_that_dies_holding_the_head_lock_does_not_block_the_chain_for_long(
    pg: PostgresStorage, pg_config: PostgresConfig
) -> None:
    """A replica killed mid-append loses its connection; PostgreSQL rolls the transaction back and frees the lock."""
    victim = await second_instance(pg_config)
    async with victim.database.connection() as conn:
        tx = conn.transaction()
        await tx.start()
        await conn.fetchrow("SELECT sequence, hash FROM audit_chain_head WHERE id = 1 FOR UPDATE")
        blocked = asyncio.create_task(pg.audit.record(actor="a", action="waits", outcome="OK"))
        await asyncio.sleep(0.3)
        assert not blocked.done()  # the other instance waits on the row lock
        conn.terminate()  # the process dies: the connection drops, nothing was committed
    record = await asyncio.wait_for(blocked, 20)
    assert record.sequence == 1 and (await pg.audit.verify()).valid
    await victim.database.close()


# ------------------------------------------------------------------------------------------------ tampering
async def admin(server: PostgresServer, schema: str) -> asyncpg.Connection:
    conn = await asyncpg.connect(host=server.host, port=server.port, user=server.user, database=server.database)
    await conn.execute(f'SET search_path TO "{schema}"')
    return conn


async def chain_of(pg: PostgresStorage, n: int = 6) -> list[AuditRecord]:
    return [await pg.audit.record(actor="a", action=f"step-{i}", outcome="OK") for i in range(n)]


@pytest.mark.security
async def test_the_database_refuses_to_update_delete_or_truncate_audit_records(pg: PostgresStorage) -> None:
    await chain_of(pg, 3)
    for statement in (
        "UPDATE audit_records SET actor = 'mallory' WHERE sequence = 2",
        "DELETE FROM audit_records WHERE sequence = 3",
        "TRUNCATE audit_records",
    ):
        with pytest.raises(asyncpg.RestrictViolationError, match="append-only"):
            await pg.database.execute(statement)
    assert (await pg.audit.verify()).valid and len(await pg.audit.list()) == 3


@pytest.mark.security
async def test_an_edited_record_breaks_the_chain(
    pg: PostgresStorage, pg_config: PostgresConfig, postgres_server: PostgresServer
) -> None:
    records = await chain_of(pg)
    victim = records[2]
    forged = json.loads(victim.model_dump_json())
    forged["outcome"] = "FORGED"
    conn = await admin(postgres_server, pg_config.schema)  # a privileged attacker who disables the trigger
    try:
        await conn.execute("ALTER TABLE audit_records DISABLE TRIGGER USER")
        await conn.execute("UPDATE audit_records SET body = $1 WHERE sequence = 3", json.dumps(forged))
    finally:
        await conn.close()
    result = await pg.audit.verify()
    assert not result.valid and result.first_invalid_sequence == 3 and result.reason == "hash chain broken"


@pytest.mark.security
async def test_a_removed_record_in_the_middle_is_a_gap(
    pg: PostgresStorage, pg_config: PostgresConfig, postgres_server: PostgresServer
) -> None:
    await chain_of(pg)
    conn = await admin(postgres_server, pg_config.schema)
    try:
        await conn.execute("ALTER TABLE audit_records DISABLE TRIGGER USER")
        await conn.execute("DELETE FROM audit_records WHERE sequence = 3")
    finally:
        await conn.close()
    result = await pg.audit.verify()
    assert not result.valid and result.reason == "sequence gap" and result.first_invalid_sequence == 4


@pytest.mark.security
async def test_records_cut_off_the_end_are_reported_even_though_the_rest_is_a_valid_chain(
    pg: PostgresStorage, pg_config: PostgresConfig, postgres_server: PostgresServer
) -> None:
    await chain_of(pg)
    conn = await admin(postgres_server, pg_config.schema)
    try:
        await conn.execute("ALTER TABLE audit_records DISABLE TRIGGER USER")
        await conn.execute("DELETE FROM audit_records WHERE sequence > 4")
    finally:
        await conn.close()
    result = await pg.audit.verify()  # the head row still says 6
    assert not result.valid and "truncated" in (result.reason or "")


@pytest.mark.security
async def test_a_consistent_rewrite_of_the_tail_is_caught_by_the_anchor(
    pg: PostgresStorage, pg_config: PostgresConfig, postgres_server: PostgresServer, tmp_path
) -> None:
    """An attacker with full database rights cuts the tail *and* moves the head: only the anchor still knows."""
    anchors = LocalAuditAnchorStore(tmp_path / "anchors")
    audit = PostgresAuditTrail(pg.database, SystemClock(), anchors)
    records = await chain_of(pg)
    assert await AuditAnchorTask(audit, anchors, SystemClock()).run() is not None  # head 6 is anchored
    assert (await audit.verify()).valid
    keep = records[3]  # sequence 4
    conn = await admin(postgres_server, pg_config.schema)
    try:
        await conn.execute("ALTER TABLE audit_records DISABLE TRIGGER USER")
        await conn.execute("DELETE FROM audit_records WHERE sequence > 4")
        await conn.execute("UPDATE audit_chain_head SET sequence = 4, hash = $1 WHERE id = 1", keep.hash)
    finally:
        await conn.close()
    result = await audit.verify()
    assert not result.valid and "behind its latest anchor" in (result.reason or "")


@pytest.mark.security
async def test_a_fully_recomputed_history_contradicts_the_anchor(
    pg: PostgresStorage, pg_config: PostgresConfig, postgres_server: PostgresServer, tmp_path
) -> None:
    """Even a chain whose every hash was recomputed is rejected: the anchored hash for sequence 6 is not its hash."""
    anchors = LocalAuditAnchorStore(tmp_path / "anchors")
    audit = PostgresAuditTrail(pg.database, SystemClock(), anchors)
    await chain_of(pg)
    await AuditAnchorTask(audit, anchors, SystemClock()).run()
    rows = await pg.database.fetch("SELECT body FROM audit_records ORDER BY sequence")
    previous, rebuilt = GENESIS_HASH, []
    for row in rows:
        record = AuditRecord.model_validate_json(row["body"])
        draft = record.model_copy(update={"outcome": "REWRITTEN", "prev_hash": previous, "hash": ""})
        record = draft.model_copy(update={"hash": record_digest(previous, draft)})
        previous = record.hash
        rebuilt.append(record)
    conn = await admin(postgres_server, pg_config.schema)
    try:
        await conn.execute("ALTER TABLE audit_records DISABLE TRIGGER USER")
        for record in rebuilt:
            await conn.execute(
                "UPDATE audit_records SET body = $1, hash = $2, prev_hash = $3, outcome = 'REWRITTEN' "
                "WHERE sequence = $4",
                record.model_dump_json(),
                record.hash,
                record.prev_hash,
                record.sequence,
            )
        await conn.execute("UPDATE audit_chain_head SET hash = $1 WHERE id = 1", previous)
    finally:
        await conn.close()
    plain = await PostgresAuditTrail(pg.database, SystemClock()).verify()  # no anchor: the rewrite is invisible
    assert plain.valid
    anchored = await audit.verify()
    assert not anchored.valid and "contradicts the audit anchor" in (anchored.reason or "")


@pytest.mark.security
async def test_conflicting_anchors_are_reported(pg: PostgresStorage, tmp_path) -> None:
    anchors = LocalAuditAnchorStore(tmp_path / "anchors")
    audit = PostgresAuditTrail(pg.database, SystemClock(), anchors)
    await chain_of(pg, 3)
    at = datetime(2026, 10, 20, tzinfo=UTC)
    await anchors.put(AuditAnchor(epoch=1, sequence=3, hash="a" * 64, anchored_at=at))
    await anchors.put(AuditAnchor(epoch=1, sequence=3, hash="b" * 64, anchored_at=at))
    result = await audit.verify()
    assert not result.valid and "Conflicting audit anchors" in (result.reason or "")


# ----------------------------------------------------------------------------------------------- restore
async def test_starting_a_new_epoch_after_a_restore_documents_the_loss_and_resumes(
    pg: PostgresStorage, tmp_path
) -> None:
    anchors = LocalAuditAnchorStore(tmp_path / "anchors")
    audit = PostgresAuditTrail(pg.database, FixedClock(datetime(2026, 10, 20, tzinfo=UTC)), anchors)
    await chain_of(pg, 5)
    await AuditAnchorTask(audit, anchors, SystemClock()).run()  # sequence 5 anchored in epoch 1
    # A restore to an earlier point: the database is behind the anchor.
    await pg.database.execute("ALTER TABLE audit_records DISABLE TRIGGER USER")
    await pg.database.execute("DELETE FROM audit_records WHERE sequence > 3")
    await pg.database.execute(
        "UPDATE audit_chain_head SET sequence = 3, hash = (SELECT hash FROM audit_records WHERE sequence = 3)"
    )
    await pg.database.execute("ALTER TABLE audit_records ENABLE TRIGGER USER")
    behind = await audit.verify()
    assert not behind.valid and "behind its latest anchor" in (behind.reason or "")

    marker = await audit.start_epoch(actor="operator:test", reason="restored to 2026-10-20T07:00Z from server old")
    assert marker.action == "audit.epoch" and marker.sequence == 4
    assert marker.details["previous_epoch"] == 1 and marker.details["database_head_sequence"] == 3
    assert marker.details["last_anchored_sequence"] == 5  # the loss, in the chain itself and in the old anchors
    assert (await audit.head()).epoch == 2
    first = await anchors.latest(2)  # starting an epoch anchors its first record: the anchors now say "epoch 2"
    assert first is not None and first.sequence == 4 and first.hash == marker.hash
    resumed = await audit.verify()
    assert resumed.valid and resumed.records == 4
    await audit.record(actor="a", action="after", outcome="OK")
    assert await AuditAnchorTask(audit, anchors, SystemClock()).run() is not None
    latest = await anchors.latest(2)
    assert latest is not None and latest.sequence == 5
    old = await anchors.latest(1)
    assert old is not None and old.sequence == 5  # the old epoch's anchors are untouched evidence of the loss
    assert (await audit.verify()).valid


async def test_a_second_restore_from_an_older_backup_does_not_reuse_an_epoch_that_has_anchors(
    pg: PostgresStorage, tmp_path
) -> None:
    anchors = LocalAuditAnchorStore(tmp_path / "anchors")
    audit = PostgresAuditTrail(pg.database, FixedClock(datetime(2026, 10, 20, tzinfo=UTC)), anchors)
    await chain_of(pg, 3)
    await AuditAnchorTask(audit, anchors, SystemClock()).run()
    await audit.start_epoch(actor="operator:test", reason="first restore")
    assert (await audit.head()).epoch == 2 and await anchors.highest_epoch() == 2
    # A later restore from a backup taken before the first one: the database says epoch 1 again.
    await pg.database.execute("UPDATE audit_chain_head SET epoch = 1")
    assert "anchors exist for audit epoch 2" in (await audit.verify()).reason  # type: ignore[operator]
    await audit.start_epoch(actor="operator:test", reason="second restore")
    assert (await audit.head()).epoch == 3  # not 2: epoch 2 already has anchors that describe another history
    assert (await audit.verify()).valid


# --------------------------------------------------------------------- findings of the independent review
@pytest.mark.security
async def test_moving_the_epoch_counter_does_not_escape_the_anchors(
    pg: PostgresStorage, pg_config: PostgresConfig, postgres_server: PostgresServer, tmp_path
) -> None:
    """A full rewrite *and* ``epoch = epoch + 1``: the rewritten chain would be checked against an epoch with no
    anchors. The anchors say which epoch the database must be in; starting an epoch is the only thing that moves it."""
    anchors = LocalAuditAnchorStore(tmp_path / "anchors")
    audit = PostgresAuditTrail(pg.database, SystemClock(), anchors)
    await chain_of(pg)
    await AuditAnchorTask(audit, anchors, SystemClock()).run()
    rows = await pg.database.fetch("SELECT body FROM audit_records ORDER BY sequence")
    previous, rebuilt = GENESIS_HASH, []
    for row in rows:
        record = AuditRecord.model_validate_json(row["body"])
        draft = record.model_copy(update={"outcome": "REWRITTEN", "prev_hash": previous, "hash": ""})
        record = draft.model_copy(update={"hash": record_digest(previous, draft)})
        previous = record.hash
        rebuilt.append(record)
    conn = await admin(postgres_server, pg_config.schema)
    try:
        await conn.execute("ALTER TABLE audit_records DISABLE TRIGGER USER")
        for record in rebuilt:
            await conn.execute(
                "UPDATE audit_records SET body = $1, hash = $2, prev_hash = $3, outcome = 'REWRITTEN' "
                "WHERE sequence = $4",
                record.model_dump_json(),
                record.hash,
                record.prev_hash,
                record.sequence,
            )
        await conn.execute("UPDATE audit_chain_head SET hash = $1, epoch = epoch + 1 WHERE id = 1", previous)
    finally:
        await conn.close()
    result = await audit.verify()
    assert not result.valid and "epoch counter was moved" in (result.reason or "")


@pytest.mark.security
async def test_an_epoch_with_no_anchor_at_all_is_not_accepted(pg: PostgresStorage, tmp_path) -> None:
    anchors = LocalAuditAnchorStore(tmp_path / "anchors")
    audit = PostgresAuditTrail(pg.database, SystemClock(), anchors)
    await chain_of(pg, 3)
    assert (await audit.verify()).valid  # epoch 1, nothing anchored yet: a fresh installation
    await pg.database.execute("UPDATE audit_chain_head SET epoch = 2")
    result = await audit.verify()
    assert not result.valid and "no anchor exists for it" in (result.reason or "")


@pytest.mark.security
async def test_a_record_slipped_in_beyond_the_head_is_reported_and_not_listed(
    pg: PostgresStorage, pg_config: PostgresConfig, postgres_server: PostgresServer
) -> None:
    records = await chain_of(pg, 3)
    last = records[-1]
    forged = AuditRecord(
        sequence=4,
        audit_id="AUD-forged",
        timestamp=last.timestamp,
        actor="mallory",
        action="forged",
        outcome="OK",
        incident_id=None,
        trace_id=None,
        details={},
        prev_hash=last.hash,
        hash="",
    )
    forged = forged.model_copy(update={"hash": record_digest(last.hash, forged)})
    conn = await admin(postgres_server, pg_config.schema)
    try:  # an INSERT is not blocked by the append-only triggers; the head row is not moved
        await conn.execute(
            "INSERT INTO audit_records (sequence, audit_id, ts, actor, action, outcome, incident_id, trace_id, "
            "prev_hash, hash, body) VALUES ($1, $2, $3, $4, $5, $6, NULL, NULL, $7, $8, $9)",
            forged.sequence,
            forged.audit_id,
            forged.timestamp,
            forged.actor,
            forged.action,
            forged.outcome,
            forged.prev_hash,
            forged.hash,
            forged.model_dump_json(),
        )
    finally:
        await conn.close()
    result = await pg.audit.verify()
    assert not result.valid and "beyond the chain head" in (result.reason or "")
    assert [r.sequence for r in await pg.audit.list()] == [1, 2, 3]  # the forged row is not part of the trail


@pytest.mark.security
async def test_columns_that_disagree_with_the_hashed_body_are_reported(
    pg: PostgresStorage, pg_config: PostgresConfig, postgres_server: PostgresServer
) -> None:
    """Queries filter on the columns, the hash covers the body: a mismatch lets a record hide from ``list``."""
    await pg.audit.record(actor="a", action="one", outcome="OK", incident_id="INC-1")
    await pg.audit.record(actor="a", action="two", outcome="OK", incident_id="INC-1")
    await pg.audit.record(actor="a", action="three", outcome="OK", incident_id="INC-1")
    conn = await admin(postgres_server, pg_config.schema)
    try:
        await conn.execute("ALTER TABLE audit_records DISABLE TRIGGER USER")
        await conn.execute("UPDATE audit_records SET incident_id = NULL, actor = 'nobody' WHERE sequence = 2")
    finally:
        await conn.close()
    assert len(await pg.audit.list(incident_id="INC-1")) == 2  # the record is hidden from this query...
    result = await pg.audit.verify()  # ...and verification says so
    assert not result.valid and result.first_invalid_sequence == 2 and "indexed columns" in (result.reason or "")


async def test_a_nul_character_is_kept_in_the_record_and_does_not_break_the_indexed_columns(
    pg: PostgresStorage,
) -> None:
    record = await pg.audit.record(actor="user\x00x", action="a", outcome="OK", incident_id="INC-\x00-1")
    assert record.actor == "user\x00x" and record.incident_id == "INC-\x00-1"  # the body keeps the exact value
    assert (await pg.audit.list(incident_id="INC-\x00-1"))[0].audit_id == record.audit_id
    assert (await pg.audit.verify()).valid
