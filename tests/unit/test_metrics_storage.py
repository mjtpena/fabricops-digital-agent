"""The storage layer's operational signals: anchor age and refused contracts reloads reach the metrics."""

from __future__ import annotations

from orqentis.observability.audit import AuditRecord, AuditVerification
from orqentis.runtime.audit_verification import AuditVerificationTask
from tests.support.metrics import MetricsHarness


class FakeAudit:
    def __init__(self) -> None:
        self.verification = AuditVerification(valid=True, records=3)

    async def verify(self) -> AuditVerification:
        return self.verification

    async def record(self, **kwargs: object) -> AuditRecord:  # pragma: no cover - not used here
        raise NotImplementedError

    async def list(self, **kwargs: object) -> list[AuditRecord]:  # pragma: no cover - not used here
        return []


class FakeAnchorSide:
    def __init__(self, age: float | None) -> None:
        self.age = age

    async def verify_anchor(self) -> AuditVerification:
        return AuditVerification(
            valid=True, records=3, has_anchor=self.age is not None, last_anchor_age_seconds=self.age
        )


async def test_the_scheduled_verification_publishes_the_age_of_the_newest_anchor() -> None:
    harness = MetricsHarness()
    side = FakeAnchorSide(1800.0)
    task = AuditVerificationTask(FakeAudit(), metrics=harness.metrics, anchor=side)  # type: ignore[arg-type]
    await task.run()
    assert harness.gauge("orqentis.audit.anchor.age") == 1800.0
    assert harness.total("orqentis.audit.verifications", result="valid") == 1

    side.age = 5400.0  # the anchor job stopped: the reading grows, and an alert on it fires above twice the interval
    await task.run()
    assert harness.gauge("orqentis.audit.anchor.age") == 5400.0

    side.age = None  # nothing anchored at all
    await task.run()
    assert harness.gauge("orqentis.audit.anchor.age") is None


def test_a_refused_contracts_reload_is_counted_by_error_type() -> None:
    harness = MetricsHarness()
    harness.metrics.contracts_reload_failed("ContractsInvalidError")
    harness.metrics.contracts_reload_failed("ContractsInvalidError")
    harness.metrics.contracts_reload_failed("ContractsFileNotFoundError")
    assert harness.total("orqentis.contracts.reload.failures", error_type="ContractsInvalidError") == 2
    assert harness.total("orqentis.contracts.reload.failures") == 3
