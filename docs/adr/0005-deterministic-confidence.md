# ADR-0005: Deterministic, explainable confidence

* Status: Accepted (Milestone 5)
* Date: 2026-10-09

## Context

LLM self-reported confidence is poorly calibrated and cannot be audited. The specification forbids
asking the LLM for an arbitrary confidence score.

## Decision

`ConfidenceCalculator` (`orqentis-weighted-signals-v1`) combines six observable signals: error
signature match (0.25), runbook match, historical match, dependency correlation, temporal
correlation and corroborating sources (0.15 each). The score is capped at 0.95 (telemetry may be
incomplete). Levels: HIGH at 0.75 or above, MEDIUM at 0.45 or above. Every factor's value,
contribution, explanation and evidence ids are stored on the incident and shown in the report.
Confidence applies to the root cause; an undetermined root cause scores 0 (LOW).

## Consequences

* Confidence is reproducible and explainable, and the platform can tune it independently of the
  model.
* Weights are heuristic and should be calibrated against resolved incidents once real history
  accumulates.
