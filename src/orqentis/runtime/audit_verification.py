"""Scheduled audit verification: tampering is found by a job, not only when someone calls ``GET /audit/verify``.

The job verifies the hash chain (every record chains to its predecessor, the sequence has no gaps, nothing was cut off
the end) and, when the storage layer provides one, the latest immutable anchor of the chain head (a published head that
a writer with access to the log could not also rewrite; WS6 / ADR-0008 adds it, and passes it in as ``anchor``).

Every run emits ``orqentis.audit.verifications{result=valid|invalid|error}``. A failure also logs one **ERROR** line,
``audit chain verification failed``, to the console log, which reaches Log Analytics through the Container Apps log
pipeline whatever the state of the metrics exporter. The "audit verification failure" alert watches both.

A run that cannot read the log (``error``) is retried a couple of times first: a reader that meets a record another
writer is half-way through appending must not page anyone. An *invalid* chain is never retried; a broken hash does not
heal.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

from orqentis.domain.principals import SYSTEM_PRINCIPAL
from orqentis.observability.audit import AuditTrail, AuditVerification
from orqentis.observability.logging import get_logger
from orqentis.observability.metrics import DISABLED, Metrics
from orqentis.observability.redaction import redact_text

_log = get_logger(__name__)

FAILURE_MESSAGE = "audit chain verification failed"
_MAX_REASON = 300


class AuditAnchorVerifier(Protocol):
    """Checks the audit chain head against the latest immutable anchor (provided with the PostgreSQL storage)."""

    async def verify_anchor(self) -> AuditVerification: ...


@dataclass(frozen=True)
class AuditCheck:
    """What one verification run found. ``failures`` names every check that failed (empty when valid)."""

    result: str  # valid | invalid | error
    records: int = 0
    failures: list[str] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return self.result == "valid"


class AuditVerificationTask:
    def __init__(
        self,
        audit: AuditTrail,
        *,
        metrics: Metrics = DISABLED,
        anchor: AuditAnchorVerifier | None = None,
        read_retries: int = 2,
        retry_delay_seconds: float = 1.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._audit = audit
        self._metrics = metrics
        self._anchor = anchor
        self._read_retries = read_retries
        self._retry_delay = retry_delay_seconds
        self._sleep = sleep
        self._last_valid: bool | None = None

    async def run(self) -> AuditCheck:
        """Verify once. Returns what was found; raises only when the log could not be read at all."""
        try:
            chain = await self._verify(self._audit.verify)
            anchor = await self._verify(self._anchor.verify_anchor) if self._anchor is not None else None
        except Exception as exc:
            self._metrics.audit_verification("error")
            _log.error(
                FAILURE_MESSAGE,
                extra={
                    "check": "read",
                    "result": "error",
                    "reason": f"{type(exc).__name__}: {redact_text(str(exc))}"[:_MAX_REASON],
                },
            )
            raise
        failures: list[str] = []
        for check, outcome in (("chain", chain), ("anchor", anchor)):
            if outcome is not None and not outcome.valid:
                failures.append(check)
                _log.error(
                    FAILURE_MESSAGE,
                    extra={
                        "check": check,
                        "result": "invalid",
                        "records": outcome.records,
                        "first_invalid_sequence": outcome.first_invalid_sequence,
                        "reason": redact_text(outcome.reason or "")[:_MAX_REASON],
                    },
                )
        if anchor is not None:
            self._metrics.audit_anchor_age(anchor.last_anchor_age_seconds)  # None (no anchor yet) clears the reading
        result = AuditCheck("invalid" if failures else "valid", chain.records, failures)
        self._metrics.audit_verification(result.result)
        if result.valid:
            _log.info("audit chain verified", extra={"records": chain.records, "anchor_checked": anchor is not None})
        await self._record_change(result)
        return result

    async def _verify(self, verify: Callable[[], Awaitable[AuditVerification]]) -> AuditVerification:
        attempts = self._read_retries + 1
        for attempt in range(1, attempts + 1):
            try:
                return await verify()
            except Exception:
                if attempt == attempts:
                    raise
                _log.warning("audit verification could not read the log; retrying", extra={"attempt": attempt})
                await self._sleep(self._retry_delay)
        raise AssertionError("unreachable")  # pragma: no cover - the loop returns or raises

    async def _record_change(self, result: AuditCheck) -> None:
        """Audit the *change* of health (first failure, recovery), never every run: the log is what is checked."""
        previous, self._last_valid = self._last_valid, result.valid
        if (previous is None and result.valid) or previous == result.valid:
            return
        try:
            await self._audit.record(
                actor=SYSTEM_PRINCIPAL.id,
                action="audit.verify",
                outcome="VERIFIED" if result.valid else "FAILED",
                details={"records": result.records, "failed_checks": result.failures},
            )
        except Exception:
            # A tampered log may refuse the append. The ERROR line and the metric already carry the news.
            _log.warning("could not record the audit verification result in the audit log", exc_info=True)
