"""PostgreSQL :class:`~orqentis.observability.audit.AuditTrail`: one hash chain for any number of writers.

**Serialisation.** The chain head (last sequence and hash) lives in one row, ``audit_chain_head``. An append runs, in a
single transaction:

1. ``SELECT sequence, hash FROM audit_chain_head WHERE id = 1 FOR UPDATE`` -- the row lock is the serialisation point:
   every other appender, on any replica, waits here until this transaction commits;
2. build the record (``sequence = head + 1``, ``prev_hash = head hash``, hash over the canonical body);
3. ``INSERT`` the record and ``UPDATE`` the head to it.

Because the lock is taken before the head is read and held until commit, no two records can ever have the same
predecessor: the chain cannot fork however many replicas append and however they interleave. If the transaction fails,
nothing was written and no sequence number was consumed. The timestamp is taken while holding the lock, from the
database's clock in deployed environments (the injected clock otherwise) and never earlier than the previous
record's, so it does not go backwards along the chain whatever the clocks of the replicas say (verification does not
depend on it).

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
from orqentis.observability.anchors import AnchorConflictError, AuditAnchorStore, ChainHead, anchor_name
from orqentis.observability.audit import GENESIS_HASH, AuditRecord, AuditVerification, record_digest
from orqentis.observability.epochs import EpochMarker, EpochMarkerStore, EpochPreconditionError, current_epoch
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
        *,
        database_clock: bool = False,
    ) -> None:
        self._db = db
        self._clock = clock
        self._anchors = anchors
        self._epochs = epochs
        self._database_clock = database_clock

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
        head = await conn.fetchrow(
            "SELECT h.sequence, h.hash, clock_timestamp() AS database_now, "
            "(SELECT r.ts FROM audit_records r WHERE r.sequence = h.sequence) AS previous_ts "
            "FROM audit_chain_head h WHERE h.id = 1 FOR UPDATE OF h"
        )
        if head is None:
            raise RuntimeError("audit_chain_head is missing: the schema was not migrated")
        sequence, previous = int(head["sequence"]) + 1, str(head["hash"])
        # The timestamp is taken while holding the head lock: from the database's clock in deployed environments (so
        # replicas whose own clocks disagree interleave one consistent time), never earlier than the previous record.
        stamped = timestamp or (head["database_now"] if self._database_clock else self._clock.now())
        if head["previous_ts"] is not None and stamped < head["previous_ts"]:
            stamped = head["previous_ts"]
        draft = AuditRecord(
            sequence=sequence,
            audit_id=new_id("AUD"),
            timestamp=stamped,
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
        return await self._verify(restore=False)

    async def _verify(self, *, restore: bool) -> AuditVerification:
        """``restore=True`` is the precondition of a new epoch: a database that is merely *behind* its anchors and
        markers (records lost by a restore) is acceptable, anything else that is wrong is not."""
        wanted: dict[int, tuple[str, str]] = {}  # sequence -> (hash that must be stored there, what says so)
        marker_sequences: dict[int, int] = {}  # sequence of an epoch-opening record -> the epoch it opens
        try:
            head = await self.head()
            markers = await self._epochs.all() if self._epochs is not None else []
            if (
                self._epochs is not None
                and head.epoch != current_epoch(markers)
                and not (restore and current_epoch(markers) > head.epoch)
            ):
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
                opens = marker_sequences.get(record.sequence)
                if opens is not None and (record.action != "audit.epoch" or record.details.get("new_epoch") != opens):
                    # An epoch is backed by its own audit.epoch record: the one its marker names, for that epoch.
                    return self._invalid(
                        expected,
                        record.sequence,
                        f"the record at sequence {record.sequence} is not the start of audit epoch {opens}",
                    )
                previous, expected, after = record.hash, expected + 1, record.sequence
        if previous != head.hash:
            return self._invalid(head.sequence, head.sequence, "the last record is not the chain head")
        # One statement, so one snapshot: records that were appended while the scan above ran are at or below the head
        # *now* and are not a problem; only a row beyond the live head, which no append could have produced, is.
        beyond = await self._db.fetchval(
            "SELECT MIN(sequence) FROM audit_records "
            "WHERE sequence > (SELECT sequence FROM audit_chain_head WHERE id = 1)"
        )
        if beyond is not None:
            return self._invalid(head.sequence, int(beyond), f"record {beyond} exists beyond the chain head")
        if wanted and max(wanted) > head.sequence and not restore:
            ahead = max(wanted)
            return self._invalid(
                head.sequence,
                ahead,
                f"audit chain is behind its latest anchor (anchored sequence {ahead}, "
                f"database head {head.sequence}): records were lost, for example by a restore",
            )
        return AuditVerification(valid=True, records=head.sequence, **await self._freshness(head.epoch))

    async def verify_anchor(self) -> AuditVerification:
        """Only the immutable side: the epoch, every anchor and marker against the stored records, and the anchor's age.

        Cheaper than :meth:`verify` (no re-derivation of the hashes): one batched read of the anchored sequences. The
        scheduled audit verification calls it in addition to the full verification.
        """
        wanted: dict[int, tuple[str, str]] = {}
        marker_sequences: dict[int, int] = {}  # sequence of an epoch-opening record -> the epoch it opens
        try:
            head = await self.head()
            markers = await self._epochs.all() if self._epochs is not None else []
            if self._epochs is not None and head.epoch != current_epoch(markers):
                return self._rejected(self._epoch_mismatch(head.epoch, current_epoch(markers)))
            if (problem := await self._collect_checks(head.epoch, markers, wanted, marker_sequences)) is not None:
                return self._rejected(problem)
        except AnchorConflictError as exc:
            return self._rejected(str(exc))
        head = await self.head()
        stored: dict[int, tuple[str, str]] = {}
        sequences = sorted(wanted)
        for start in range(0, len(sequences), _PAGE):
            batch = sequences[start : start + _PAGE]
            rows = await self._db.fetch(
                "SELECT sequence, hash, action FROM audit_records WHERE sequence = ANY($1)", batch
            )
            stored |= {int(r["sequence"]): (str(r["hash"]), str(r["action"])) for r in rows}
        for sequence in sequences:
            digest, what = wanted[sequence]
            if sequence > head.sequence:
                return self._invalid(
                    head.sequence,
                    sequence,
                    f"audit chain is behind its latest anchor (anchored sequence {max(wanted)}, "
                    f"database head {head.sequence}): records were lost, for example by a restore",
                )
            if sequence not in stored or stored[sequence][0] != digest:
                return self._invalid(
                    head.sequence, sequence, f"hash chain contradicts the {what} for sequence {sequence}"
                )
            if sequence in marker_sequences and stored[sequence][1] != "audit.epoch":
                return self._invalid(
                    head.sequence, sequence, f"the record at sequence {sequence} is not an epoch start"
                )
        return AuditVerification(valid=True, records=head.sequence, **await self._freshness(head.epoch))

    async def _freshness(self, epoch: int) -> dict[str, Any]:
        if self._anchors is None:
            return {}
        try:
            latest = await self._anchors.latest(epoch)
        except AnchorConflictError:
            return {}
        if latest is None:
            return {"has_anchor": False}
        age = max(0.0, (self._clock.now() - latest.anchored_at).total_seconds())
        return {"has_anchor": True, "last_anchor_sequence": latest.sequence, "last_anchor_age_seconds": age}

    async def _collect_checks(
        self,
        epoch: int,
        markers: Sequence[EpochMarker],
        wanted: dict[int, tuple[str, str]],
        marker_sequences: dict[int, int],
    ) -> str | None:
        """What the immutable side says the chain must contain: every anchor and epoch marker that still applies.

        * The records an earlier epoch anchored at or beyond the sequence where the next epoch opened are the ones the
          restore lost; they are evidence, not a claim on the chain that replaced them.
        * A marker whose record is not in the chain is **unused** when a later epoch was opened over it (an operator
          run whose commit failed, and the retry): it neither pins anything nor cuts anything off. The later marker, and
          the hash chain, which makes every later hash depend on every earlier record, cover the same ground, so a
          rewrite of an old epoch record is still caught by them. The current epoch's marker always counts.
        * Anchors that an operator disregarded when opening an epoch (``EpochMarker.disregarded_anchors``) are skipped,
          but only by markers that are used: an unused marker was never audited in the chain, so it disregards nothing.
        """
        stored: dict[int, str] = {}
        sequences = sorted({m.sequence for m in markers if m.epoch <= epoch})
        for start in range(0, len(sequences), _PAGE):
            rows = await self._db.fetch(
                "SELECT sequence, hash FROM audit_records WHERE sequence = ANY($1)", sequences[start : start + _PAGE]
            )
            stored |= {int(r["sequence"]): str(r["hash"]) for r in rows}
        used = [m for m in markers if m.epoch <= epoch and (m.epoch == epoch or stored.get(m.sequence) == m.hash)]
        disregarded = frozenset(name for m in used for name in m.disregarded_anchors)

        def lost(item_epoch: int, sequence: int) -> bool:
            later = [m.sequence for m in used if m.epoch > item_epoch]
            return item_epoch < epoch and bool(later) and sequence >= min(later)

        def add(sequence: int, digest: str, what: str) -> str | None:
            known = wanted.get(sequence)
            if known is not None and known[0] != digest:
                return f"Conflicting audit anchors for sequence {sequence}: {sorted([known[0][:12], digest[:12]])}"
            wanted[sequence] = (digest, what)
            return None

        for marker in used:
            if not lost(marker.epoch, marker.sequence):
                marker_sequences[marker.sequence] = marker.epoch
                if (problem := add(marker.sequence, marker.hash, f"epoch {marker.epoch} marker")) is not None:
                    return problem
        if self._anchors is not None:
            for k in range(1, epoch + 1) if self._epochs is not None else [epoch]:
                for ref in await self._anchors.refs(k):
                    if lost(k, ref.sequence) or anchor_name(ref) in disregarded:
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
    async def start_epoch(
        self,
        *,
        actor: str,
        reason: str,
        override_verification: str | None = None,
        disregard_anchors: Sequence[str] = (),
    ) -> AuditRecord:
        """Record, in the chain, that history was cut (a restore) and begin a new epoch. An operator action.

        The new epoch's **marker is written first, inside the transaction that appends the ``audit.epoch`` record**, to
        a container the application's identity can only read: with the application's identity this raises, the
        transaction rolls back and the epoch does not change. With an operator's identity both happen or neither does
        (a crash between the marker and the commit leaves an unused marker, which verification only reports until the
        next ``start-epoch``: that picks the next epoch number, and the unused marker, whose record never existed in the
        chain, then pins nothing, even if records were appended in between).

        ``disregard_anchors`` names anchors (``epoch-NNNN/<sequence>-<hash>.json``) that are junk, for example written
        by a compromised application identity. They cannot be deleted before their retention ends and a low-sequence
        one would fail verification for ever, so the new marker records that they are disregarded. This is accepting a
        failed verification and therefore needs ``override_verification``; the names are checked to exist and written
        to the marker (immutable, with who and why) and to the audit record.

        The record names the head the restored database has and the last anchor of the epoch that ends, so the loss is
        documented in the chain itself and in the immutable anchors of the old epoch, which are never removed.
        """
        if self._epochs is None:
            raise RuntimeError("starting an audit epoch needs the epoch marker store (the audit-epochs container)")
        # Precondition: do not bless a database that is tampered with. A restore leaves the chain behind its anchors,
        # which is exactly what an epoch is for; anything else wrong (a broken hash, a record that contradicts an
        # anchor, a moved epoch counter) refuses, unless an operator overrides with a reason that is itself audited.
        if disregard_anchors and override_verification is None:
            raise EpochPreconditionError(
                "disregarding anchors accepts a failed verification: give --override-verification with the reason"
            )
        verification = await self._verify(restore=True)
        if not verification.valid:
            if override_verification is None:
                raise EpochPreconditionError(verification.reason or "the audit chain is not consistent")
            _log.error(
                "audit epoch started over a failed verification",
                extra={"reason": verification.reason, "override": redact_text(override_verification)[:300]},
            )
        previous_anchor = None
        head = await self.head()
        markers = await self._epochs.all()
        new_epoch = max(head.epoch, current_epoch(markers)) + 1
        if disregard_anchors:
            known = (
                {anchor_name(r) for k in range(1, head.epoch + 1) for r in await self._anchors.refs(k)}
                if self._anchors
                else set()
            )
            if missing := sorted(set(disregard_anchors) - known):
                raise EpochPreconditionError(f"no such anchor to disregard: {', '.join(missing)}")
        if self._anchors is not None:
            try:
                previous_anchor = await self._anchors.latest(head.epoch, exclude=disregard_anchors)
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
        if not verification.valid:
            details["verification_override"] = override_verification
            details["verification_failure"] = verification.reason
        if disregard_anchors:
            details["disregarded_anchors"] = sorted(disregard_anchors)
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
                    disregarded_anchors=tuple(sorted(disregard_anchors)),
                )
            )
        return record
