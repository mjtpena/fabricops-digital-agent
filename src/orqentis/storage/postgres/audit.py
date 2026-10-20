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
beyond it. With the immutable side configured it also checks that the database is in the epoch of the highest **epoch
marker** (a marker only an operator can write, :mod:`orqentis.observability.epochs`) and that the record at **every**
anchored sequence has the anchored hash (rewrites, tail deletions and a chain regrown past its anchor each contradict an
anchor in storage the database cannot change; see :mod:`orqentis.observability.anchors`). Tampering is detectable up to
the last anchor; the records after it are protected by the hash chain alone. Records appended while verification runs
are simply beyond the head it started from.

Audit appends are never fenced: an audit record describes something that happened, and it is more important that it is
kept than that a deposed leader's late record is refused.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from orqentis.config.clock import Clock
from orqentis.domain.common import new_id
from orqentis.observability.anchors import AnchorConflictError, AuditAnchorStore, ChainHead
from orqentis.observability.audit import GENESIS_HASH, AuditRecord, AuditVerification, record_digest
from orqentis.observability.epochs import EpochMarker, EpochMarkerStore, current_epoch
from orqentis.observability.logging import get_logger
from orqentis.observability.redaction import redact, redact_text
from orqentis.storage.postgres.database import Connection, PostgresDatabase

_log = get_logger(__name__)

_PAGE = 1000


def _column_text(value: str | None) -> str | None:
    """Text for an indexed column. PostgreSQL text cannot hold NUL; the exact value stays in the (JSON) body."""
    return value.replace("\x00", "\ufffd") if value is not None else None


class PostgresAuditTrail:
    def __init__(
        self,
        db: PostgresDatabase,
        clock: Clock,
        anchors: AuditAnchorStore | None = None,
        epochs: EpochMarkerStore | None = None,
    ) -> None:
        self._db = db
        self._clock = clock
        self._anchors = anchors
        self._epochs = epochs

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

    async def record_hash_at(self, sequence: int) -> str | None:
        value = await self._db.fetchval("SELECT hash FROM audit_records WHERE sequence = $1", sequence)
        return str(value) if value is not None else None

    # -------------------------------------------------------------------------------------------- verification
    async def verify(self) -> AuditVerification:
        """Hash chain, indexed columns, head, epoch, every anchor and every epoch marker; see the module docstring."""
        wanted: dict[int, tuple[str, str]] = {}  # sequence -> (hash that must be stored there, what says so)
        marker_sequences: set[int] = set()
        try:
            head = await self.head()
            markers = await self._epochs.all() if self._epochs is not None else []
            if self._epochs is not None and head.epoch != current_epoch(markers):
                return self._rejected(self._epoch_mismatch(head.epoch, current_epoch(markers)))
            if (problem := await self._collect_checks(head.epoch, markers, wanted, marker_sequences)) is not None:
                return self._rejected(problem)
        except AnchorConflictError as exc:
            return self._rejected(str(exc))
        head = await self.head()  # anchors and markers were read first: none of them can be ahead of this head

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
                if (anchored := wanted.get(record.sequence)) is not None and anchored[0] != record.hash:
                    return self._invalid(
                        expected,
                        record.sequence,
                        f"hash chain contradicts the {anchored[1]} for sequence {record.sequence}",
                    )
                if record.sequence in marker_sequences and record.action != "audit.epoch":
                    return self._invalid(
                        expected, record.sequence, f"the record at sequence {record.sequence} is not an epoch start"
                    )
                previous, expected, after = record.hash, expected + 1, record.sequence
        if previous != head.hash:
            return self._invalid(head.sequence, head.sequence, "the last record is not the chain head")
        beyond = await self._db.fetchval("SELECT MIN(sequence) FROM audit_records WHERE sequence > $1", head.sequence)
        if beyond is not None:
            return self._invalid(
                head.sequence, int(beyond), f"record {beyond} exists beyond the chain head (sequence {head.sequence})"
            )
        if wanted and max(wanted) > head.sequence:
            ahead = max(wanted)
            return self._invalid(
                head.sequence,
                ahead,
                f"audit chain is behind its latest anchor (anchored sequence {ahead}, "
                f"database head {head.sequence}): records were lost, for example by a restore",
            )
        return AuditVerification(valid=True, records=head.sequence)

    async def _collect_checks(
        self,
        epoch: int,
        markers: Sequence[EpochMarker],
        wanted: dict[int, tuple[str, str]],
        marker_sequences: set[int],
    ) -> str | None:
        """What the immutable side says the chain must contain: every anchor and epoch marker that still applies.

        The records an earlier epoch anchored at or beyond the sequence where the next epoch opened are the ones the
        restore lost; they are evidence, not a claim on the chain that replaced them.
        """

        def lost(item_epoch: int, sequence: int) -> bool:
            later = [m.sequence for m in markers if m.epoch > item_epoch]
            return item_epoch < epoch and bool(later) and sequence >= min(later)

        def add(sequence: int, digest: str, what: str) -> str | None:
            known = wanted.get(sequence)
            if known is not None and known[0] != digest:
                return f"Conflicting audit anchors for sequence {sequence}: {sorted([known[0][:12], digest[:12]])}"
            wanted[sequence] = (digest, what)
            return None

        for marker in markers:
            if marker.epoch <= epoch and not lost(marker.epoch, marker.sequence):
                marker_sequences.add(marker.sequence)
                if (problem := add(marker.sequence, marker.hash, f"epoch {marker.epoch} marker")) is not None:
                    return problem
        if self._anchors is not None:
            for k in range(1, epoch + 1) if self._epochs is not None else [epoch]:
                for ref in await self._anchors.refs(k):
                    if lost(k, ref.sequence):
                        continue
                    if (problem := add(ref.sequence, ref.hash, f"audit anchor (epoch {k})")) is not None:
                        return problem
        return None

    @staticmethod
    def _epoch_mismatch(database: int, markers: int) -> str:
        if database > markers:
            return (
                f"the database claims audit epoch {database} but the latest epoch marker is {markers}: "
                "the epoch counter was moved without `orqentis audit start-epoch`"
            )
        return (
            f"epoch markers exist for audit epoch {markers} but the database is in epoch {database}: "
            "it was restored or rewound without `orqentis audit start-epoch`"
        )

    @staticmethod
    def _rejected(reason: str) -> AuditVerification:
        return AuditVerification(valid=False, records=0, first_invalid_sequence=None, reason=reason)

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

    # ------------------------------------------------------------------------------------------------ epochs
    async def start_epoch(self, *, actor: str, reason: str) -> AuditRecord:
        """Record, in the chain, that history was cut (a restore) and begin a new epoch. An operator action.

        The new epoch's **marker is written first, inside the transaction that appends the ``audit.epoch`` record**, to
        a container the application's identity can only read: with the application's identity this raises, the
        transaction rolls back and the epoch does not change. With an operator's identity both happen or neither does
        (a crash between the marker and the commit leaves an unused marker; the next ``start-epoch`` picks the next
        epoch number and verification ignores the unused one, because its record never existed in the chain).

        The record names the head the restored database has and the last anchor of the epoch that ends, so the loss is
        documented in the chain itself and in the immutable anchors of the old epoch, which are never removed.
        """
        if self._epochs is None:
            raise RuntimeError("starting an audit epoch needs the epoch marker store (the audit-epochs container)")
        previous_anchor = None
        head = await self.head()
        markers = await self._epochs.all()
        new_epoch = max(head.epoch, current_epoch(markers)) + 1
        if self._anchors is not None:
            try:
                previous_anchor = await self._anchors.latest(head.epoch)
            except AnchorConflictError as exc:
                _log.warning("conflicting anchors while starting a new audit epoch", extra={"error": str(exc)})
        details: dict[str, Any] = {
            "reason": reason,
            "previous_epoch": head.epoch,
            "new_epoch": new_epoch,
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
                details=redact(details),
                timestamp=None,
                new_epoch=new_epoch,
            )
            # Before the commit: no epoch exists without a marker for it. A refusal (the application's read-only
            # identity) or a failure here rolls the transaction back.
            await self._epochs.put(
                EpochMarker(
                    epoch=new_epoch,
                    sequence=record.sequence,
                    hash=record.hash,
                    started_at=self._clock.now(),
                    started_by=redact_text(actor)[:200],
                    reason=redact_text(reason)[:500],
                )
            )
        return record
