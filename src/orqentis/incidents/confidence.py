"""Deterministic confidence model.

The LLM is never asked for a confidence number. Confidence in a diagnosis is computed from
observable signals, and every contributing factor is stored with the incident.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from orqentis.domain.confidence import ConfidenceAssessment, ConfidenceFactor, ConfidenceLevel

METHOD = "orqentis-weighted-signals-v1"
MAX_SCORE = 0.95  # never certain: telemetry can be incomplete
HIGH_THRESHOLD = 0.75
MEDIUM_THRESHOLD = 0.45

WEIGHTS: dict[str, float] = {
    "error_signature_match": 0.25,
    "runbook_match": 0.15,
    "historical_incident_match": 0.15,
    "dependency_correlation": 0.15,
    "temporal_correlation": 0.15,
    "corroborating_sources": 0.15,
}


@dataclass
class Signal:
    value: float
    explanation: str
    evidence_ids: list[str] = field(default_factory=list)


@dataclass
class ConfidenceSignals:
    """Each value in [0, 1]. Missing signals count as 0 (absence of evidence lowers confidence)."""

    error_signature_match: Signal | None = None
    runbook_match: Signal | None = None
    historical_incident_match: Signal | None = None
    dependency_correlation: Signal | None = None
    temporal_correlation: Signal | None = None
    corroborating_sources: Signal | None = None


def corroboration_value(distinct_sources: int) -> float:
    return min(distinct_sources, 3) / 3


def level_for(score: float) -> ConfidenceLevel:
    if score >= HIGH_THRESHOLD:
        return ConfidenceLevel.HIGH
    if score >= MEDIUM_THRESHOLD:
        return ConfidenceLevel.MEDIUM
    return ConfidenceLevel.LOW


class ConfidenceCalculator:
    def __init__(self, weights: dict[str, float] | None = None) -> None:
        self._weights = dict(weights or WEIGHTS)
        total = sum(self._weights.values())
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"Confidence weights must sum to 1.0 (got {total})")

    def assess(self, signals: ConfidenceSignals) -> ConfidenceAssessment:
        factors: list[ConfidenceFactor] = []
        raw = 0.0
        for name, weight in self._weights.items():
            signal: Signal | None = getattr(signals, name)
            value = max(0.0, min(1.0, signal.value)) if signal else 0.0
            contribution = round(weight * value, 4)
            raw += contribution
            factors.append(
                ConfidenceFactor(
                    name=name,
                    weight=weight,
                    value=round(value, 3),
                    contribution=contribution,
                    explanation=signal.explanation if signal else "No supporting signal observed",
                    evidence_ids=signal.evidence_ids if signal else [],
                )
            )
        score = round(min(raw, MAX_SCORE), 3)
        return ConfidenceAssessment(
            score=score, level=level_for(score), factors=factors, method=METHOD, capped=raw > MAX_SCORE
        )
