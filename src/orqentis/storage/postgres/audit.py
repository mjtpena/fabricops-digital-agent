"""PostgreSQL :class:`~orqentis.observability.audit.AuditTrail`: one hash chain for any number of writers.

**Serialisation.** The chain head (last sequence and hash) lives in one row, ``audit_chain_head``. An append runs, in a
single transaction:

1. ``SELECT sequence, hash FROM audit_chain_head WHERE id = 1 FOR UPDATE`` -- the row lock is the serialisation point:
   every other appender, on any replica, waits here until this transaction commits;
2. build the record (``sequence = head + 1``, ``prev_hash = head hash``, hash over the canonical body);
3. ``INSERT`` the record and ``UPDATE`` the head to it.

Because the lock is taken before the head is read and held until commit, no two records can ever have the same
predecessor: the chain cannot fork however many replicas append and however they interleave. If the transaction fails,
nothing was written and no sequence number was consumed. The timestamp is taken from the injected clock while holding
the lock (so, with synchronised clocks, it grows with the sequence; verification does not depend on it).

**Verification** re-derives every hash from the stored record bodies (exact JSON text), checks that sequences are
contiguous, that the last record is the chain head, and, when an anchor store is configured, that the record the
latest anchor of the current epoch names exists with the anchored hash (rewrites and tail deletions contradict an
anchor in storage the database cannot change; see :mod:`orqentis.observability.anchors`). Records appended while
verification runs are simply beyond the head it started from.

Audit appends are never fenced: an audit record describes something that happened, and it is more important that it is
kept than that a deposed leader's late record is refused.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from orqentis.config.clock import Clock
from orqentis.domain.common import new_id
from orqentis.observability.anchors import AnchorConflictError, AuditAnchorStore, ChainHead
from orqentis.observability.audit import GENESIS_HASH, AuditRecord, AuditVerification, record_digest
from orqentis.observability.logging import get_logger
from orqentis.observability.redaction import redact
from orqentis.storage.postgres.database import Connection, PostgresDatabase

_log = get_logger(__name__)

_PAGE = 1000


class PostgresAuditTrail:
    def __init__(self, db: PostgresDatabase, clock: Clock, anchors: AuditAnchorStore | None = None) -> None:
        self._db = db
        self._clock = clock
        self._anchors = anchors

    # ---------------------------------------------------------------------------------------------- append
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
        safe_details = redact(details or {})
        async with self._db.transaction(fenced=False) as conn:
            return await self._append(
                conn,
                actor=actor,
                action=action,
                outcome=outcome,
                incident_id=incident_id,
                trace_id=trace_id,
                details=safe_details,
                timestamp=timestamp,
            )

    async def _append(
        self,
        conn: Connection,
        *,
        actor: str,
        action: str,
        outcome: str,
        incident_id: str | None,
        trace_id: str | None,
        details: dict[str, Any],
        timestamp: datetime | None,
        new_epoch: bool = False,
    ) -> AuditRecord:
        head = await conn.fetchrow("SELECT sequence, hash FROM audit_chain_head WHERE id = 1 FOR UPDATE")
        if head is None:
            raise RuntimeError("audit_chain_head is missing: the schema was not migrated")
        sequence, previous = int(head["sequence"]) + 1, str(head["hash"])
        draft = AuditRecord(
            sequence=sequence,
            audit_id=new_id("AUD"),
            timestamp=timestamp or self._clock.now(),
            actor=actor,
            action=action,
            outcome=outcome,
            incident_id=incident_id,
            trace_id=trace_id,
            details=details,
            prev_hash=previous,
            hash="",
        )
        record = draft.model_copy(update={"hash": record_digest(previous, draft)})
        await conn.execute(
            "INSERT INTO audit_records (sequence, audit_id, ts, actor, action, outcome, incident_id, trace_id, "
            "prev_hash, hash, body) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)",
            record.sequence,
            record.audit_id,
            record.timestamp,
            record.actor,
            record.action,
            record.outcome,
            record.incident_id,
            record.trace_id,
            record.prev_hash,
            record.hash,
            record.model_dump_json(),
        )
        await conn.execute(
            "UPDATE audit_chain_head SET sequence = $1, hash = $2, epoch = epoch + $3 WHERE id = 1",
            record.sequence,
            record.hash,
            1 if new_epoch else 0,
        )
        return record

    # ------------------------------------------------------------------------------------------------ reads
    async def head(self) -> ChainHead:
        row = await self._db.fetchrow("SELECT epoch, sequence, hash FROM audit_chain_head WHERE id = 1")
        if row is None:
            raise RuntimeError("audit_chain_head is missing: the schema was not migrated")
        return ChainHead(epoch=int(row["epoch"]), sequence=int(row["sequence"]), hash=str(row["hash"]))

    async def list(self, *, incident_id: str | None = None, limit: int | None = None) -> list[AuditRecord]:
        args: list[Any] = []
        where = ""
        if incident_id is not None:
            args.append(incident_id)
            where = "WHERE incident_id = $1"
        if limit:
            args.append(limit)
            sql = f"SELECT body FROM audit_records {where} ORDER BY sequence DESC LIMIT ${len(args)}"  # noqa: S608
            rows = list(reversed(await self._db.fetch(sql, *args)))
        else:
            rows = await self._db.fetch(f"SELECT body FROM audit_records {where} ORDER BY sequence", *args)  # noqa: S608
        return [AuditRecord.model_validate_json(r["body"]) for r in rows]

    # -------------------------------------------------------------------------------------------- verification
    async def verify(self) -> AuditVerification:
        anchor_hash_by_sequence: dict[int, str] = {}
        head = await self.head()
        anchor = None
        if self._anchors is not None:
            try:
                anchor = await self._anchors.latest(head.epoch)  # read first: it cannot be ahead of a later head
            except AnchorConflictError as exc:
                return AuditVerification(valid=False, records=0, first_invalid_sequence=None, reason=str(exc))
        if anchor is not None:
            anchor_hash_by_sequence[anchor.sequence] = anchor.hash
        head = await self.head()

        previous, expected, after = GENESIS_HASH, 1, 0
        while expected <= head.sequence:
            rows = await self._db.fetch(
                "SELECT sequence, body FROM audit_records WHERE sequence > $1 AND sequence <= $2 "
                "ORDER BY sequence LIMIT $3",
                after,
                head.sequence,
                _PAGE,
            )
            if not rows:
                return self._invalid(
                    expected - 1,
                    expected,
                    f"log truncated: records from sequence {expected} to {head.sequence} are missing",
                )
            for row in rows:
                record = AuditRecord.model_validate_json(row["body"])
                if record.sequence != expected or int(row["sequence"]) != expected:
                    return self._invalid(expected - 1, record.sequence, "sequence gap")
                if record.prev_hash != previous or record_digest(previous, record) != record.hash:
                    return self._invalid(expected - 1, record.sequence, "hash chain broken")
                if anchor_hash_by_sequence.get(record.sequence, record.hash) != record.hash:
                    return self._invalid(
                        expected,
                        record.sequence,
                        f"hash chain contradicts the audit anchor for sequence {record.sequence}",
                    )
                previous, expected, after = record.hash, expected + 1, record.sequence
        if previous != head.hash:
            return self._invalid(head.sequence, head.sequence, "the last record is not the chain head")
        if anchor is not None and anchor.sequence > head.sequence:
            return self._invalid(
                head.sequence,
                anchor.sequence,
                f"audit chain is behind its latest anchor (anchored sequence {anchor.sequence}, "
                f"database head {head.sequence}): records were lost, for example by a restore",
            )
        return AuditVerification(valid=True, records=head.sequence)

    @staticmethod
    def _invalid(records: int, first_invalid: int, reason: str) -> AuditVerification:
        return AuditVerification(valid=False, records=records, first_invalid_sequence=first_invalid, reason=reason)

    # ------------------------------------------------------------------------------------------------ epochs
    async def start_epoch(self, *, actor: str, reason: str) -> AuditRecord:
        """Record, in the chain, that history was cut (a restore) and begin a new anchor epoch.

        The record names the head the restored database has and the last anchor of the epoch that ends, so the loss
        is documented in the chain itself and in the immutable anchors of the old epoch, which are never removed.
        """
        previous_anchor = None
        head = await self.head()
        if self._anchors is not None:
            try:
                previous_anchor = await self._anchors.latest(head.epoch)
            except AnchorConflictError as exc:
                _log.warning("conflicting anchors while starting a new audit epoch", extra={"error": str(exc)})
        details: dict[str, Any] = {
            "reason": reason,
            "previous_epoch": head.epoch,
            "database_head_sequence": head.sequence,
            "database_head_hash": head.hash,
            "last_anchored_sequence": previous_anchor.sequence if previous_anchor else None,
            "last_anchored_hash": previous_anchor.hash if previous_anchor else None,
        }
        async with self._db.transaction(fenced=False) as conn:
            return await self._append(
                conn,
                actor=actor,
                action="audit.epoch",
                outcome="STARTED",
                incident_id=None,
                trace_id=None,
                details=redact(details),
                timestamp=None,
                new_epoch=True,
            )
