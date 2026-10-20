"""Helpers for tests that attack the audit chain the way someone with full database rights would."""

from __future__ import annotations

import asyncpg

from orqentis.observability.audit import GENESIS_HASH, AuditRecord, record_digest
from orqentis.storage.postgres import PostgresStorage
from tests.support.services import PostgresServer


async def admin(server: PostgresServer, schema: str) -> asyncpg.Connection:
    """A superuser connection on the schema: it can disable the append-only triggers and edit anything."""
    conn = await asyncpg.connect(host=server.host, port=server.port, user=server.user, database=server.database)
    await conn.execute(f'SET search_path TO "{schema}"')
    return conn


async def chain_of(pg: PostgresStorage, n: int = 6, *, start: int = 0) -> list[AuditRecord]:
    return [await pg.audit.record(actor="a", action=f"step-{i}", outcome="OK") for i in range(start, start + n)]


async def recompute_history(
    server: PostgresServer, schema: str, pg: PostgresStorage, *, outcome: str = "REWRITTEN", epoch: int | None = None
) -> list[AuditRecord]:
    """Rewrite every record and recompute every hash (and the head), optionally moving the epoch counter too."""
    rows = await pg.database.fetch("SELECT body FROM audit_records ORDER BY sequence")
    previous, rebuilt = GENESIS_HASH, []
    for row in rows:
        record = AuditRecord.model_validate_json(row["body"])
        draft = record.model_copy(update={"outcome": outcome, "prev_hash": previous, "hash": ""})
        record = draft.model_copy(update={"hash": record_digest(previous, draft)})
        previous = record.hash
        rebuilt.append(record)
    conn = await admin(server, schema)
    try:
        await conn.execute("ALTER TABLE audit_records DISABLE TRIGGER USER")
        for record in rebuilt:
            await conn.execute(
                "UPDATE audit_records SET body = $1, hash = $2, prev_hash = $3, outcome = $4 WHERE sequence = $5",
                record.model_dump_json(),
                record.hash,
                record.prev_hash,
                outcome,
                record.sequence,
            )
        await conn.execute(
            "UPDATE audit_chain_head SET hash = $1, epoch = COALESCE($2::integer, epoch) WHERE id = 1", previous, epoch
        )
        await conn.execute("ALTER TABLE audit_records ENABLE TRIGGER USER")
    finally:
        await conn.close()
    return rebuilt


async def cut_back_to(server: PostgresServer, schema: str, sequence: int) -> None:
    """What a restore to an earlier point does to the chain: records after ``sequence`` are gone, the head follows."""
    conn = await admin(server, schema)
    try:
        await conn.execute("ALTER TABLE audit_records DISABLE TRIGGER USER")
        await conn.execute("DELETE FROM audit_records WHERE sequence > $1", sequence)
        await conn.execute(
            "UPDATE audit_chain_head SET sequence = $1, hash = (SELECT hash FROM audit_records WHERE sequence = $1) "
            "WHERE id = 1",
            sequence,
        )
        await conn.execute("ALTER TABLE audit_records ENABLE TRIGGER USER")
    finally:
        await conn.close()
