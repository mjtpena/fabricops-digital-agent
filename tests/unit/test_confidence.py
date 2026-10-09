"""Deterministic confidence calculation."""

from __future__ import annotations

import pytest

from orqentis.domain.confidence import ConfidenceLevel
from orqentis.incidents.confidence import (
    MAX_SCORE,
    WEIGHTS,
    ConfidenceCalculator,
    ConfidenceSignals,
    Signal,
    corroboration_value,
)


def full(value: float = 1.0) -> ConfidenceSignals:
    return ConfidenceSignals(**{name: Signal(value, name, [f"EV-{name}"]) for name in WEIGHTS})


def test_weights_sum_to_one() -> None:
    assert sum(WEIGHTS.values()) == pytest.approx(1.0)


def test_all_signals_give_capped_high_confidence() -> None:
    result = ConfidenceCalculator().assess(full())
    assert result.score == MAX_SCORE
    assert result.capped is True
    assert result.level is ConfidenceLevel.HIGH
    assert {f.name for f in result.factors} == set(WEIGHTS)
    assert all(f.evidence_ids for f in result.factors)


def test_no_signals_give_zero_low_confidence() -> None:
    result = ConfidenceCalculator().assess(ConfidenceSignals())
    assert result.score == 0.0
    assert result.level is ConfidenceLevel.LOW
    assert all(f.explanation == "No supporting signal observed" for f in result.factors)


@pytest.mark.parametrize(
    ("value", "level"), [(0.8, ConfidenceLevel.HIGH), (0.5, ConfidenceLevel.MEDIUM), (0.3, ConfidenceLevel.LOW)]
)
def test_levels(value: float, level: ConfidenceLevel) -> None:
    assert ConfidenceCalculator().assess(full(value)).level is level


def test_signature_only_is_low_confidence() -> None:
    signals = ConfidenceSignals(error_signature_match=Signal(1.0, "signature"))
    result = ConfidenceCalculator().assess(signals)
    assert result.score == pytest.approx(0.25)
    assert result.level is ConfidenceLevel.LOW


def test_values_are_clamped() -> None:
    result = ConfidenceCalculator().assess(ConfidenceSignals(runbook_match=Signal(7.0, "too big")))
    assert result.score == pytest.approx(0.15)


def test_corroboration_value() -> None:
    assert [corroboration_value(n) for n in (0, 1, 2, 3, 9)] == pytest.approx([0, 1 / 3, 2 / 3, 1, 1])


def test_invalid_weights_rejected() -> None:
    with pytest.raises(ValueError, match=r"sum to 1\.0"):
        ConfidenceCalculator({"error_signature_match": 0.5})
