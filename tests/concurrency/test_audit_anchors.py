"""Anchors and epochs: tampering is found up to the last anchor, and only an operator can open an epoch.

Each test is an attack that was reproduced against the first version of the anchors (which compared only the latest
anchor and trusted an epoch counter that lives in the database), or a part of the restore procedure that
``docs/operations/restore.md`` relies on.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from tests.support.audit_tamper import chain_of, cut_back_to, recompute_history
from tests.support.postgres_app import ReadOnlyEpochMarkerStore
from tests.support.services import PostgresServer

from orqentis.config.clock import FixedClock, SystemClock
from orqentis.observability.anchors import (
    AUDIT_VERIFICATION_FAILURE_MESSAGE,
    AnchorRefusedError,
    AuditAnchor,
    AuditAnchorTask,
    ChainHead,
    LocalAuditAnchorStore,
)
from orqentis.observability.epochs import EpochMarker, EpochPreconditionError, LocalEpochMarkerStore
from orqentis.storage.postgres import PostgresAuditTrail, PostgresConfig, PostgresStorage

AT = datetime(2026, 10, 20, 8, 0, tzinfo=UTC)


class Setup:
    """An audit trail with an anchor store and an epoch marker store, and the task that anchors it."""

    def __init__(self, pg: PostgresStorage, tmp_path: Path, *, epochs: object | None = None) -> None:
        self.pg = pg
        self.anchors = LocalAuditAnchorStore(tmp_path / "anchors")
        self.epoch_store = LocalEpochMarkerStore(tmp_path / "epochs")
        # The operator's view: it can write markers. The application's view of the same container cannot.
        self.operator = PostgresAuditTrail(pg.database, FixedClock(AT), self.anchors, self.epoch_store)
        self.app = PostgresAuditTrail(
            pg.database, FixedClock(AT), self.anchors, ReadOnlyEpochMarkerStore(self.epoch_store)
        )
        self.failures: list[str] = []
        self.task = AuditAnchorTask(
            self.app, self.anchors, SystemClock(), epochs=self.epoch_store, on_failure=self.failures.append
        )

    async def anchor(self) -> AuditAnchor | None:
        return await self.task.run()


@pytest.fixture
def setup(pg: PostgresStorage, tmp_path: Path) -> Setup:
    return Setup(pg, tmp_path)


# ----------------------------------------------------------------------------------------- every anchor binds
@pytest.mark.security
async def test_rewriting_everything_appending_and_anchoring_again_does_not_launder_the_rewrite(
    setup: Setup, pg_config: PostgresConfig, postgres_server: PostgresServer, caplog: pytest.LogCaptureFixture
) -> None:
    """Full hash recompute, one new record, then the next scheduled anchor: the old anchor still contradicts it."""
    await chain_of(setup.pg)
    assert await setup.anchor() is not None  # head 6 is anchored
    await recompute_history(postgres_server, pg_config.schema, setup.pg)
    await setup.pg.audit.record(actor="a", action="after-the-rewrite", outcome="OK")  # head 7, on a rewritten chain

    with caplog.at_level(logging.ERROR), pytest.raises(AnchorRefusedError, match="not the one that was anchored"):
        await setup.anchor()  # the task refuses to put a rewritten head into the immutable record...
    assert [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR] == [
        AUDIT_VERIFICATION_FAILURE_MESSAGE
    ]
    assert len(setup.failures) == 1
    assert [r.sequence for r in await setup.anchors.refs(1)] == [6]  # ...so only the honest anchor exists

    result = await setup.app.verify()  # ...and verification, which checks every anchor, says so
    assert not result.valid and "contradicts the audit anchor" in (result.reason or "")

    # Even if the rewritten head did get anchored (an older task, a compromised app), the older anchor still binds.
    head = await setup.pg.audit.head()
    await setup.anchors.put(AuditAnchor(epoch=1, sequence=head.sequence, hash=head.hash, anchored_at=AT))
    later = await setup.app.verify()
    assert not later.valid and "contradicts the audit anchor (epoch 1) for sequence 6" in (later.reason or "")


@pytest.mark.security
async def test_cutting_the_tail_and_growing_a_different_chain_past_the_anchor_is_caught(
    setup: Setup, pg_config: PostgresConfig, postgres_server: PostgresServer
) -> None:
    await chain_of(setup.pg)
    await setup.anchor()  # sequence 6
    await cut_back_to(postgres_server, pg_config.schema, 3)
    for n in range(5):  # sequence 4..8: a different history, longer than the anchored one
        await setup.pg.audit.record(actor="mallory", action=f"other-{n}", outcome="OK")
    assert (await setup.pg.audit.head()).sequence == 8

    result = await setup.app.verify()
    assert not result.valid and "contradicts the audit anchor (epoch 1) for sequence 6" in (result.reason or "")
    with pytest.raises(AnchorRefusedError):
        await setup.anchor()


@pytest.mark.security
async def test_restoring_without_opening_an_epoch_is_refused_by_the_anchor_task_and_reported_by_verification(
    setup: Setup, pg_config: PostgresConfig, postgres_server: PostgresServer
) -> None:
    await chain_of(setup.pg)
    await setup.anchor()
    await cut_back_to(postgres_server, pg_config.schema, 4)
    behind = await setup.app.verify()
    assert not behind.valid and "behind its latest anchor" in (behind.reason or "")
    with pytest.raises(AnchorRefusedError, match="behind the latest anchor"):
        await setup.anchor()  # nothing new is anchored while the chain disagrees with what was anchored
    await setup.pg.audit.record(actor="a", action="grown", outcome="OK")
    await setup.pg.audit.record(actor="a", action="grown-again", outcome="OK")
    await setup.pg.audit.record(actor="a", action="and-again", outcome="OK")  # now past sequence 6
    with pytest.raises(AnchorRefusedError, match="not the one that was anchored"):
        await setup.anchor()
    assert not (await setup.app.verify()).valid


@pytest.mark.security
async def test_moving_the_epoch_counter_does_not_escape_the_anchors_or_get_anchored(
    setup: Setup, pg_config: PostgresConfig, postgres_server: PostgresServer
) -> None:
    """A full rewrite *and* ``epoch = epoch + 1``: no epoch marker exists for it, and the app cannot write one."""
    await chain_of(setup.pg)
    await setup.anchor()
    await recompute_history(postgres_server, pg_config.schema, setup.pg, epoch=2)

    result = await setup.app.verify()
    assert not result.valid and "epoch counter was moved" in (result.reason or "")
    with pytest.raises(AnchorRefusedError, match="epoch markers say 1"):
        await setup.anchor()  # one anchor run does not make it valid again
    assert not (await setup.app.verify()).valid
    assert await setup.anchors.refs(2) == []


@pytest.mark.security
async def test_a_forged_marker_file_is_the_only_way_to_move_the_epoch_and_the_app_cannot_write_one(
    setup: Setup,
) -> None:
    await chain_of(setup.pg, 3)
    with pytest.raises(PermissionError):
        await setup.app.start_epoch(actor="operator:mallory", reason="the app tries to open an epoch")
    head = await setup.pg.audit.head()
    assert head.epoch == 1 and head.sequence == 3  # refused atomically: no record, no epoch change
    assert list(await setup.epoch_store.all()) == []
    assert (await setup.app.verify()).valid


# ------------------------------------------------------------------------------------------- the restore procedure
@pytest.mark.security
async def test_a_restore_with_an_operator_opened_epoch_resumes_and_keeps_binding_the_surviving_history(
    setup: Setup, pg_config: PostgresConfig, postgres_server: PostgresServer
) -> None:
    records = await chain_of(setup.pg, 6)
    await setup.anchor()  # sequence 6, epoch 1
    await cut_back_to(postgres_server, pg_config.schema, 4)  # restore point: records 5 and 6 are lost
    assert not (await setup.app.verify()).valid  # behind the anchor until an operator records the loss

    marker_record = await setup.operator.start_epoch(actor="operator:olivia", reason="PITR restore to 07:00Z")
    assert marker_record.action == "audit.epoch" and marker_record.sequence == 5
    assert marker_record.details["last_anchored_sequence"] == 6  # the loss is documented in the chain itself
    (marker,) = await setup.epoch_store.all()
    assert (marker.epoch, marker.sequence, marker.hash) == (2, 5, marker_record.hash)
    assert (await setup.pg.audit.head()).epoch == 2

    resumed = await setup.app.verify()  # the epoch-1 anchor at sequence 6 describes records the restore lost
    assert resumed.valid and resumed.records == 5
    await setup.pg.audit.record(actor="a", action="after-the-restore", outcome="OK")
    assert await setup.anchor() is not None  # the app anchors again, in epoch 2
    assert [r.sequence for r in await setup.anchors.refs(2)] == [6]
    assert [r.sequence for r in await setup.anchors.refs(1)] == [6]  # the old epoch's anchor is untouched evidence
    assert (await setup.app.verify()).valid

    # The surviving prefix stays bound by the old epoch's anchors: one at sequence 4 would catch a rewrite of it.
    await setup.anchors.put(AuditAnchor(epoch=1, sequence=4, hash=records[3].hash, anchored_at=AT))
    assert (await setup.app.verify()).valid
    await setup.anchors.put(AuditAnchor(epoch=1, sequence=3, hash="c" * 64, anchored_at=AT))
    forged = await setup.app.verify()
    assert not forged.valid and "contradicts the audit anchor" in (forged.reason or "")


@pytest.mark.security
async def test_a_second_restore_from_an_older_backup_opens_the_next_free_epoch(
    setup: Setup, pg_config: PostgresConfig, postgres_server: PostgresServer
) -> None:
    await chain_of(setup.pg, 4)
    await setup.anchor()
    await setup.operator.start_epoch(actor="operator:olivia", reason="first restore")
    assert (await setup.pg.audit.head()).epoch == 2
    # A later restore from a backup taken before the first one: the database says epoch 1 again.
    await setup.pg.database.execute("UPDATE audit_chain_head SET epoch = 1")
    result = await setup.app.verify()
    assert not result.valid and "restored or rewound" in (result.reason or "")
    await setup.operator.start_epoch(actor="operator:olivia", reason="second restore")
    assert (await setup.pg.audit.head()).epoch == 3  # not 2: epoch 2 already has a marker
    assert [m.epoch for m in await setup.epoch_store.all()] == [2, 3]
    assert (await setup.app.verify()).valid


@pytest.mark.security
async def test_an_unused_marker_from_an_operator_run_that_did_not_commit_is_ignored_once_the_next_epoch_exists(
    setup: Setup,
) -> None:
    await chain_of(setup.pg, 3)
    await setup.anchor()
    # The operator's marker was written, then the transaction did not commit: a marker for epoch 2 and a database in 1.
    await setup.epoch_store.put(
        EpochMarker(
            epoch=2, sequence=4, hash="d" * 64, started_at=AT, started_by="operator:olivia", reason="did not commit"
        )
    )
    result = await setup.app.verify()
    assert not result.valid and "restored or rewound" in (result.reason or "")
    with pytest.raises(AnchorRefusedError):
        await setup.anchor()  # nothing is anchored until an operator runs start-epoch again

    record = await setup.operator.start_epoch(actor="operator:olivia", reason="second attempt")
    assert record.sequence == 4 and (await setup.pg.audit.head()).epoch == 3
    assert (await setup.app.verify()).valid  # the unused marker (epoch 2, sequence 4) is no claim on the chain
    anchor = await setup.anchor()
    assert anchor is not None and anchor.epoch == 3 and anchor.sequence == 4


@pytest.mark.security
async def test_a_marker_that_names_another_record_than_the_one_in_the_chain_is_reported(setup: Setup) -> None:
    await chain_of(setup.pg, 3)
    record = await setup.operator.start_epoch(actor="operator:olivia", reason="restore")
    assert (await setup.app.verify()).valid
    (marker,) = await setup.epoch_store.all()
    assert marker.hash == record.hash
    # Someone rewrites the epoch record (and the rest of the chain) in the database: the marker no longer matches.
    await setup.pg.database.execute("ALTER TABLE audit_records DISABLE TRIGGER USER")
    await setup.pg.database.execute(
        "UPDATE audit_records SET hash = $1, body = replace(body, $2, $3) WHERE sequence = $4",
        "e" * 64,
        record.hash,
        "e" * 64,
        record.sequence,
    )
    await setup.pg.database.execute("UPDATE audit_chain_head SET hash = $1 WHERE id = 1", "e" * 64)
    assert not (await setup.app.verify()).valid


# ---------------------------------------------------------------------------------------- the anchor task refuses
@pytest.mark.security
async def test_the_anchor_task_keeps_refusing_until_the_chain_matches_its_anchors_again(setup: Setup) -> None:
    await chain_of(setup.pg, 3)
    first = await setup.anchor()
    assert first is not None and first.sequence == 3
    await setup.pg.audit.record(actor="a", action="honest", outcome="OK")
    second = await setup.anchor()
    assert second is not None and second.sequence == 4  # an honest chain keeps being anchored
    assert setup.failures == []

    await setup.anchors.put(
        AuditAnchor(epoch=1, sequence=4, hash="f" * 64, anchored_at=AT)
    )  # a second, conflicting one
    await setup.pg.audit.record(actor="a", action="more", outcome="OK")
    for _ in range(2):
        with pytest.raises(AnchorRefusedError, match="Conflicting audit anchors"):
            await setup.anchor()
    assert len(setup.failures) == 2  # every run says so; the scheduler reports the job as failed each time


async def test_a_database_with_no_anchors_and_no_markers_is_a_fresh_installation(setup: Setup) -> None:
    await chain_of(setup.pg, 3)
    assert (await setup.app.verify()).valid
    anchor = await setup.anchor()
    assert anchor is not None and anchor.sequence == 3


# ------------------------------------------------------------------------------- the precondition of a new epoch
@pytest.mark.security
async def test_an_epoch_is_refused_over_a_tampered_chain_unless_an_operator_overrides_and_that_is_audited(
    setup: Setup, pg_config: PostgresConfig, postgres_server: PostgresServer, caplog: pytest.LogCaptureFixture
) -> None:
    """``start-epoch`` must not bless whatever the database holds: a rewritten chain is not a restore."""
    await chain_of(setup.pg)
    await setup.anchor()
    await recompute_history(postgres_server, pg_config.schema, setup.pg)

    with pytest.raises(EpochPreconditionError, match="contradicts the audit anchor"):
        await setup.operator.start_epoch(actor="operator:olivia", reason="looks like a restore")
    head = await setup.pg.audit.head()
    assert head.epoch == 1 and head.sequence == 6  # nothing appended, nothing moved
    assert list(await setup.epoch_store.all()) == []  # and no marker

    with caplog.at_level(logging.ERROR):
        record = await setup.operator.start_epoch(
            actor="operator:olivia", reason="restore", override_verification="INC-77: the anchors were re-created"
        )
    assert record.details["verification_override"] == "INC-77: the anchors were re-created"
    assert "contradicts the audit anchor" in str(record.details["verification_failure"])
    assert "audit epoch started over a failed verification" in caplog.text
    assert (await setup.pg.audit.head()).epoch == 2


@pytest.mark.security
async def test_an_epoch_is_refused_when_the_counter_was_moved_forward_without_a_marker(
    setup: Setup, pg_config: PostgresConfig, postgres_server: PostgresServer
) -> None:
    await chain_of(setup.pg, 3)
    await setup.pg.database.execute("UPDATE audit_chain_head SET epoch = 5")
    with pytest.raises(EpochPreconditionError, match="epoch counter was moved"):
        await setup.operator.start_epoch(actor="operator:olivia", reason="restore")


# ------------------------------------------------------------------------------------------------------ freshness
async def test_a_valid_chain_says_how_old_its_newest_anchor_is_and_a_missing_anchor_is_not_silent(
    pg: PostgresStorage, tmp_path: Path
) -> None:
    clock = FixedClock(AT)
    anchors = LocalAuditAnchorStore(tmp_path / "anchors")
    audit = PostgresAuditTrail(pg.database, clock, anchors, LocalEpochMarkerStore(tmp_path / "epochs"))
    await chain_of(pg, 3)
    none_yet = await audit.verify()
    assert none_yet.valid and none_yet.has_anchor is False and none_yet.last_anchor_age_seconds is None

    task = AuditAnchorTask(audit, anchors, clock, stale_after=timedelta(minutes=30))
    await task.run()
    fresh = await audit.verify()
    assert fresh.valid and fresh.has_anchor and fresh.last_anchor_sequence == 3 and fresh.last_anchor_age_seconds == 0
    quick = await audit.verify_anchor()
    assert quick.valid and quick.last_anchor_age_seconds == 0

    clock.set(AT + timedelta(minutes=45))  # the anchor job stopped (a missing storage role, a crashed leader)
    stale = await audit.verify()
    assert stale.valid and stale.last_anchor_age_seconds == 45 * 60  # valid, and says how long nothing was anchored
    assert task.stale  # and the anchoring itself reports that it has not worked for twice the interval
    await task.run()  # a run that works again clears it
    assert not task.stale


async def test_the_anchor_only_verification_finds_what_the_full_one_finds(
    setup: Setup, pg_config: PostgresConfig, postgres_server: PostgresServer
) -> None:
    await chain_of(setup.pg)
    await setup.anchor()
    assert (await setup.app.verify_anchor()).valid
    await recompute_history(postgres_server, pg_config.schema, setup.pg)
    quick = await setup.app.verify_anchor()
    assert not quick.valid and "contradicts the audit anchor" in (quick.reason or "")
    await cut_back_to(postgres_server, pg_config.schema, 3)
    assert "behind its latest anchor" in ((await setup.app.verify_anchor()).reason or "")


@pytest.mark.security
async def test_an_epoch_marker_must_name_the_audit_epoch_record_that_opened_that_very_epoch(setup: Setup) -> None:
    await chain_of(setup.pg, 3)
    opened = await setup.operator.start_epoch(actor="operator:olivia", reason="restore")
    assert (await setup.app.verify()).valid
    # A marker for epoch 3 that points at the (real, hashed, audit.epoch) record that opened epoch 2, and a counter
    # moved to 3: the hash matches and the record is an epoch start, but it is not the start of epoch 3.
    await setup.epoch_store.put(
        EpochMarker(
            epoch=3,
            sequence=opened.sequence,
            hash=opened.hash,
            started_at=AT,
            started_by="operator:mallory",
            reason="reuse an old epoch record",
        )
    )
    await setup.pg.database.execute("UPDATE audit_chain_head SET epoch = 3")
    result = await setup.app.verify()
    assert not result.valid and "is not the start of audit epoch" in (result.reason or "")


# ------------------------------------------------------------------------------------- unused markers, junk anchors
@pytest.mark.security
async def test_an_unused_marker_is_harmless_even_when_records_were_appended_before_the_retry(setup: Setup) -> None:
    """An operator run whose commit failed leaves a marker for an epoch that never started; the app keeps appending."""
    await chain_of(setup.pg, 3)
    await setup.anchor()  # sequence 3, epoch 1
    await setup.epoch_store.put(
        EpochMarker(
            epoch=2, sequence=4, hash="d" * 64, started_at=AT, started_by="operator:olivia", reason="did not commit"
        )
    )
    await chain_of(setup.pg, 3, start=10)  # records 4..6 are appended before the operator comes back
    stuck = await setup.app.verify()
    assert not stuck.valid and "restored or rewound" in (stuck.reason or "")

    opened = await setup.operator.start_epoch(actor="operator:olivia", reason="second attempt")
    assert opened.sequence == 7 and (await setup.pg.audit.head()).epoch == 3
    resumed = await setup.app.verify()
    assert resumed.valid, resumed.reason  # the unused marker (epoch 2, sequence 4) pins nothing

    # ...and it does not cut anything off: an epoch-1 anchor in the range it names (>= 4) still binds the chain.
    await setup.anchors.put(AuditAnchor(epoch=1, sequence=5, hash="e" * 64, anchored_at=AT))
    wrong = await setup.app.verify()
    assert not wrong.valid and "contradicts the audit anchor (epoch 1) for sequence 5" in (wrong.reason or "")


@pytest.mark.security
async def test_a_junk_anchor_can_only_be_set_aside_by_an_operator_over_an_audited_override_and_stays_in_the_store(
    setup: Setup,
) -> None:
    await chain_of(setup.pg, 6)
    await setup.anchor()
    junk = AuditAnchor(epoch=1, sequence=2, hash="b" * 64, anchored_at=AT)  # written by a compromised app identity
    await setup.anchors.put(junk)
    assert not (await setup.app.verify()).valid  # it contradicts record 2, and it can never be removed

    with pytest.raises(EpochPreconditionError, match="contradicts"):
        await setup.operator.start_epoch(actor="operator:olivia", reason="clean up")
    with pytest.raises(EpochPreconditionError, match="override"):
        await setup.operator.start_epoch(actor="operator:olivia", reason="x", disregard_anchors=[junk.name])
    with pytest.raises(EpochPreconditionError, match="no such anchor"):
        await setup.operator.start_epoch(
            actor="operator:olivia",
            reason="x",
            override_verification="INC-9",
            disregard_anchors=["epoch-0001/000000000099-" + "a" * 64 + ".json"],
        )
    assert (await setup.pg.audit.head()).epoch == 1 and list(await setup.epoch_store.all()) == []

    # An override without naming the anchor opens an epoch but the junk anchor still fails verification for ever.
    # (Not done here: it would consume the epoch; the next assertions cover the way that works.)
    record = await setup.operator.start_epoch(
        actor="operator:olivia",
        reason="junk anchor from a compromised identity (INC-9)",
        override_verification="INC-9: the application identity was compromised and wrote anchor 2",
        disregard_anchors=[junk.name],
    )
    assert record.details["disregarded_anchors"] == [junk.name] and "verification_override" in record.details
    (marker,) = await setup.epoch_store.all()
    assert marker.disregarded_anchors == (junk.name,) and marker.started_by == "operator:olivia"
    assert (
        await setup.app.verify()
    ).valid  # the junk anchor is still in the store, and no longer held against the chain
    assert [r.sequence for r in await setup.anchors.refs(1)] == [2, 6]

    await setup.pg.audit.record(actor="a", action="after", outcome="OK")
    assert await setup.anchor() is not None  # anchoring goes on in the new epoch
    assert (await setup.app.verify()).valid


class StaleHeadThenFresh:
    """An audit trail whose first read of the head is out of date: another leader appended and anchored meanwhile."""

    def __init__(self, stale_sequence: int, real_sequence: int, real_hash: str) -> None:
        self._reads = 0
        self._stale, self._real, self._hash = stale_sequence, real_sequence, real_hash

    async def head(self) -> ChainHead:
        self._reads += 1
        return ChainHead(epoch=1, sequence=self._stale if self._reads == 1 else self._real, hash=self._hash)

    async def record_hash_at(self, sequence: int) -> str | None:
        return self._hash if sequence == self._real else "0" * 64


async def test_the_anchor_task_does_not_refuse_an_untampered_chain_at_a_leader_handover(tmp_path: Path) -> None:
    """Reading the head before the anchors made another leader's newer anchor look like 'records were lost'."""
    store = LocalAuditAnchorStore(tmp_path / "anchors")
    real = "a" * 64
    await store.put(AuditAnchor(epoch=1, sequence=6, hash=real, anchored_at=AT))  # the previous leader, just now
    task = AuditAnchorTask(StaleHeadThenFresh(5, 6, real), store, SystemClock())
    assert await task.run() is None  # nothing new to anchor (head 6 is anchored), and no refusal


@pytest.mark.security
async def test_a_marker_that_never_started_its_epoch_disregards_nothing(setup: Setup) -> None:
    """The disregard is audited by the ``audit.epoch`` record; a marker that never got one has not been audited."""
    await chain_of(setup.pg, 6)
    await setup.anchor()
    junk = AuditAnchor(epoch=1, sequence=2, hash="b" * 64, anchored_at=AT)
    await setup.anchors.put(junk)
    await setup.epoch_store.put(
        EpochMarker(
            epoch=2,
            sequence=7,
            hash="d" * 64,
            started_at=AT,
            started_by="operator:olivia",
            reason="commit failed",
            disregarded_anchors=(junk.name,),
        )
    )
    await setup.operator.start_epoch(actor="operator:olivia", reason="retry", override_verification="INC-9: retry")
    result = await setup.app.verify()
    assert not result.valid and "contradicts the audit anchor (epoch 1) for sequence 2" in (result.reason or "")
