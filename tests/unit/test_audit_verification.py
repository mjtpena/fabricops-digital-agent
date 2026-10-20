"""The scheduled audit verification job: a tampered chain is reported, a healthy one is quiet, a flaky read retried."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from orqentis.config.clock import FixedClock
from orqentis.observability.audit import AuditVerification, LocalAuditLog
from orqentis.runtime.audit_verification import FAILURE_MESSAGE, AuditVerificationTask
from tests.support.metrics import MetricsHarness

T0 = datetime(2026, 10, 20, 8, 0, tzinfo=UTC)


class LogCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def errors(self) -> list[logging.LogRecord]:
        return [r for r in self.records if r.levelno >= logging.ERROR]


@pytest.fixture
def logs() -> Iterator[LogCapture]:
    """Capture the job's own logger directly: ``configure_logging`` in other tests may stop propagation."""
    capture = LogCapture()
    logger = logging.getLogger("orqentis.runtime.audit_verification")
    previous_level = logger.level
    logger.setLevel(logging.DEBUG)
    logger.addHandler(capture)
    try:
        yield capture
    finally:
        logger.removeHandler(capture)
        logger.setLevel(previous_level)


async def audit_log(tmp_path: Path, records: int = 5) -> LocalAuditLog:
    log = LocalAuditLog(tmp_path / "audit" / "audit.jsonl", FixedClock(T0))
    for i in range(records):
        await log.record(actor="system:test", action="test.event", outcome=f"OK-{i}", details={"n": i})
    return log


def tamper(log_path: Path, line_index: int) -> None:
    """Change one character of a record without changing the file size, the way a quiet edit would."""
    lines = log_path.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[line_index])
    record["outcome"] = record["outcome"][:-1] + "X"
    lines[line_index] = json.dumps(record)
    log_path.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))


async def no_sleep(_: float) -> None:
    return None


async def test_a_healthy_chain_is_reported_valid_and_stays_quiet(tmp_path: Path, logs: LogCapture) -> None:
    h = MetricsHarness()
    log = await audit_log(tmp_path)
    task = AuditVerificationTask(log, metrics=h.metrics)

    check = await task.run()

    assert check.valid and check.result == "valid" and check.records == 5 and check.failures == []
    assert h.total("orqentis.audit.verifications", result="valid") == 1
    assert h.total("orqentis.audit.verifications", result="invalid") == 0
    assert logs.errors() == []
    assert [r for r in await log.list() if r.action == "audit.verify"] == []  # a healthy run writes nothing


async def test_a_tampered_chain_is_reported_with_a_metric_and_an_error_log_line(
    tmp_path: Path, logs: LogCapture
) -> None:
    h = MetricsHarness()
    log = await audit_log(tmp_path)
    task = AuditVerificationTask(log, metrics=h.metrics)
    tamper(tmp_path / "audit" / "audit.jsonl", 2)

    check = await task.run()

    assert not check.valid and check.result == "invalid" and check.failures == ["chain"]
    assert h.total("orqentis.audit.verifications", result="invalid") == 1
    assert h.total("orqentis.audit.verifications", result="valid") == 0
    (error,) = logs.errors()
    assert error.getMessage() == FAILURE_MESSAGE  # the line the alert looks for
    assert (error.__dict__["check"], error.__dict__["first_invalid_sequence"]) == ("chain", 3)
    assert "hash chain broken" in error.__dict__["reason"]


async def test_records_cut_off_the_end_of_the_log_are_reported(tmp_path: Path, logs: LogCapture) -> None:
    h = MetricsHarness()
    path = tmp_path / "audit" / "audit.jsonl"
    log = await audit_log(tmp_path)
    task = AuditVerificationTask(log, metrics=h.metrics)
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join(lines[:-2]) + "\n", encoding="utf-8")  # the newest two records vanish

    check = await task.run()  # must not raise even though the log now refuses to append

    assert check.result == "invalid" and check.failures == ["chain"]
    assert h.total("orqentis.audit.verifications", result="invalid") == 1
    assert "truncated" in logs.errors()[0].__dict__["reason"]


async def test_a_failure_is_audited_once_when_health_changes_not_on_every_run(tmp_path: Path, logs: LogCapture) -> None:
    h = MetricsHarness()
    log = await audit_log(tmp_path)
    task = AuditVerificationTask(log, metrics=h.metrics)
    assert (await task.run()).valid  # healthy: nothing is written

    tamper(tmp_path / "audit" / "audit.jsonl", 1)
    for _ in range(3):
        assert not (await task.run()).valid

    changes = [r for r in await log.list() if r.action == "audit.verify"]
    assert [(r.outcome, r.actor) for r in changes] == [("FAILED", "system:orqentis")]
    assert h.total("orqentis.audit.verifications", result="invalid") == 3  # the metric still counts every run
    assert len(logs.errors()) == 3


class FakeAnchor:
    def __init__(self, valid: bool) -> None:
        self.valid = valid
        self.calls = 0

    async def verify_anchor(self) -> AuditVerification:
        self.calls += 1
        return (
            AuditVerification(valid=True, records=5)
            if self.valid
            else AuditVerification(valid=False, records=5, first_invalid_sequence=6, reason="head differs from anchor")
        )


async def test_a_chain_that_no_longer_matches_the_immutable_anchor_is_reported(
    tmp_path: Path, logs: LogCapture
) -> None:
    h = MetricsHarness()
    log = await audit_log(tmp_path)
    anchor = FakeAnchor(valid=False)
    task = AuditVerificationTask(log, metrics=h.metrics, anchor=anchor)

    check = await task.run()

    assert check.result == "invalid" and check.failures == ["anchor"]  # the chain itself is intact
    assert anchor.calls == 1
    assert logs.errors()[0].__dict__["check"] == "anchor"
    assert h.total("orqentis.audit.verifications", result="invalid") == 1

    good = AuditVerificationTask(log, metrics=h.metrics, anchor=FakeAnchor(valid=True))
    assert (await good.run()).valid


class FlakyAudit:
    """``verify`` raises ``failures`` times (a record half written by another writer), then answers."""

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    async def verify(self) -> AuditVerification:
        self.calls += 1
        if self.calls <= self.failures:
            raise ValueError("unterminated record")
        return AuditVerification(valid=True, records=3)

    async def record(self, **_: object) -> None:  # pragma: no cover - never reached on a valid chain
        raise AssertionError("a valid chain must not be audited")


async def test_a_read_that_fails_once_is_retried_and_does_not_page_anyone(logs: LogCapture) -> None:
    h = MetricsHarness()
    audit = FlakyAudit(failures=2)
    task = AuditVerificationTask(audit, metrics=h.metrics, read_retries=2, sleep=no_sleep)  # type: ignore[arg-type]

    check = await task.run()

    assert check.valid and audit.calls == 3
    assert h.total("orqentis.audit.verifications", result="valid") == 1
    assert h.total("orqentis.audit.verifications", result="error") == 0
    assert logs.errors() == []


async def test_a_log_that_cannot_be_read_is_an_error_result_and_the_job_fails(logs: LogCapture) -> None:
    h = MetricsHarness()
    audit = FlakyAudit(failures=99)
    task = AuditVerificationTask(audit, metrics=h.metrics, read_retries=2, sleep=no_sleep)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="unterminated"):
        await task.run()  # the scheduler records the job failure

    assert audit.calls == 3
    assert h.total("orqentis.audit.verifications", result="error") == 1
    (error,) = logs.errors()
    assert error.getMessage() == FAILURE_MESSAGE and error.__dict__["check"] == "read"
