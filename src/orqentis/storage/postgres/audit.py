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
contiguous, that the indexed columns agree with the body, that the last record is the chain head and that nothing exists
beyond it, and, when an anchor store is configured, that the database is in the highest epoch that has an anchor and
that the record the latest anchor of that epoch names exists with the anchored hash (rewrites and tail deletions
contradict an anchor in storage the database cannot change; see :mod:`orqentis.observability.anchors`). Records appended
while verification runs are simply beyond the head it started from.

Audit appends are never fenced: an audit record describes something that happened, and it is more important that it is
kept than that a deposed leader's late record is refused.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from orqentis.config.clock import Clock
from orqentis.domain.common import new_id
from orqentis.observability.anchors import AnchorConflictError, AuditAnchor, AuditAnchorStore, ChainHead
from orqentis.observability.audit import GENESIS_HASH, AuditRecord, AuditVerification, record_digest
from orqentis.observability.logging import get_logger
from orqentis.observability.redaction import redact
from orqentis.storage.postgres.database import Connection, PostgresDatabase

_log = get_logger(__name__)

_PAGE = 1000


def _column_text(value: str | None) -> str | None:
    """Text for an indexed column. PostgreSQL text cannot hold NUL; the exact value stays in the (JSON) body."""
    return value.replace("\x00", "\ufffd") if value is not None else None


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
        new_epoch: int | None = None,
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
            _column_text(record.actor),
            _column_text(record.action),
            _column_text(record.outcome),
            _column_text(record.incident_id),
            _column_text(record.trace_id),
            record.prev_hash,
            record.hash,
            record.model_dump_json(),
        )
        await conn.execute(
            "UPDATE audit_chain_head SET sequence = $1, hash = $2, epoch = COALESCE($3::integer, epoch) WHERE id = 1",
            record.sequence,
            record.hash,
            new_epoch,
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
        # Nothing beyond the chain head is part of the trail (a row that was slipped in is reported by verify()).
        conditions = ["sequence <= (SELECT sequence FROM audit_chain_head WHERE id = 1)"]
        if incident_id is not None:
            args.append(_column_text(incident_id))
            conditions.append("incident_id = $1")
        where = "WHERE " + " AND ".join(conditions)
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
                if (problem := await self._check_epoch(head.epoch)) is not None:
                    return AuditVerification(valid=False, records=0, first_invalid_sequence=None, reason=problem)
                anchor = await self._anchors.latest(head.epoch)  # read first: it cannot be ahead of a later head
            except AnchorConflictError as exc:
                return AuditVerification(valid=False, records=0, first_invalid_sequence=None, reason=str(exc))
        if anchor is not None:
            anchor_hash_by_sequence[anchor.sequence] = anchor.hash
        head = await self.head()

        previous, expected, after = GENESIS_HASH, 1, 0
        while expected <= head.sequence:
            rows = await self._db.fetch(
                "SELECT sequence, audit_id, ts, actor, action, outcome, incident_id, trace_id, prev_hash, hash, body "
                "FROM audit_records WHERE sequence > $1 AND sequence <= $2 ORDER BY sequence LIMIT $3",
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
                if not self._columns_match(row, record):
                    return self._invalid(
                        expected - 1, record.sequence, "indexed columns disagree with the hashed record body"
                    )
                if anchor_hash_by_sequence.get(record.sequence, record.hash) != record.hash:
                    return self._invalid(
                        expected,
                        record.sequence,
                        f"hash chain contradicts the audit anchor for sequence {record.sequence}",
                    )
                previous, expected, after = record.hash, expected + 1, record.sequence
        if previous != head.hash:
            return self._invalid(head.sequence, head.sequence, "the last record is not the chain head")
        beyond = await self._db.fetchval("SELECT MIN(sequence) FROM audit_records WHERE sequence > $1", head.sequence)
        if beyond is not None:
            return self._invalid(
                head.sequence, int(beyond), f"record {beyond} exists beyond the chain head (sequence {head.sequence})"
            )
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

    @staticmethod
    def _columns_match(row: Any, record: AuditRecord) -> bool:
        """The columns that queries filter on must be the values that were hashed."""
        return bool(
            row["audit_id"] == record.audit_id
            and row["ts"] == record.timestamp
            and row["actor"] == _column_text(record.actor)
            and row["action"] == _column_text(record.action)
            and row["outcome"] == _column_text(record.outcome)
            and row["incident_id"] == _column_text(record.incident_id)
            and row["trace_id"] == _column_text(record.trace_id)
            and row["prev_hash"] == record.prev_hash
            and row["hash"] == record.hash
        )

    async def _check_epoch(self, epoch: int) -> str | None:
        """The database must be in the highest epoch that has an anchor.

        The epoch counter is a column the database owner can change. The anchors cannot be changed, and starting an
        epoch writes its first anchor, so a counter that disagrees with the anchors was moved without that operator
        action: either to dodge the anchors of the real epoch, or by restoring an older database.
        """
        assert self._anchors is not None
        highest = await self._anchors.highest_epoch()
        if highest is None:
            if epoch > 1:
                return f"the database is in audit epoch {epoch} but no anchor exists for it"
            return None
        if highest > epoch:
            return (
                f"anchors exist for audit epoch {highest} but the database is in epoch {epoch}: "
                "it was restored or rewound without `orqentis audit start-epoch`"
            )
        if highest < epoch:
            return (
                f"the database claims audit epoch {epoch} but the latest anchored epoch is {highest}: "
                "the epoch counter was moved without starting an epoch"
            )
        return None

    # ------------------------------------------------------------------------------------------------ epochs
    async def start_epoch(self, *, actor: str, reason: str) -> AuditRecord:
        """Record, in the chain, that history was cut (a restore) and begin a new anchor epoch.

        The record names the head the restored database has and the last anchor of the epoch that ends, so the loss
        is documented in the chain itself and in the immutable anchors of the old epoch, which are never removed.
        """
        previous_anchor = None
        head = await self.head()
        new_epoch = head.epoch + 1
        if self._anchors is not None:
            try:
                previous_anchor = await self._anchors.latest(head.epoch)
            except AnchorConflictError as exc:
                _log.warning("conflicting anchors while starting a new audit epoch", extra={"error": str(exc)})
            # Never reuse an epoch that already has anchors (a second restore from an older backup).
            new_epoch = max(new_epoch, (await self._anchors.highest_epoch() or 0) + 1)
        details: dict[str, Any] = {
            "reason": reason,
            "previous_epoch": head.epoch,
            "database_head_sequence": head.sequence,
            "database_head_hash": head.hash,
            "last_anchored_sequence": previous_anchor.sequence if previous_anchor else None,
            "last_anchored_hash": previous_anchor.hash if previous_anchor else None,
        }
        async with self._db.transaction(fenced=False) as conn:
            record = await self._append(
                conn,
                actor=actor,
                action="audit.epoch",
                outcome="STARTED",
                incident_id=None,
                trace_id=None,
                details=redact({**details, "new_epoch": new_epoch}),
                timestamp=None,
                new_epoch=new_epoch,
            )
        if self._anchors is not None:
            # The first anchor of the new epoch is this record: from now on the anchors, which the database cannot
            # change, say which epoch the database must be in. If this write fails the next scheduled anchor writes
            # one for the same epoch (verify() reports "no anchor" until then); it is not retried here.
            await self._anchors.put(
                AuditAnchor(epoch=new_epoch, sequence=record.sequence, hash=record.hash, anchored_at=self._clock.now())
            )
        return record
