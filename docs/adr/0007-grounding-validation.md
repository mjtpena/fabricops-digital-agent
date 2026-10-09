# ADR-0007: Accept agent output only when grounded in evidence

* Status: Accepted (Milestone 5)
* Date: 2026-10-09

## Context

The agent must reason from evidence and never invent telemetry. It may say that evidence is
insufficient.

## Decision

`GroundingValidator` post-processes every `AgentDiagnosis`:

* findings and the root cause must cite evidence ids present on the incident (including evidence
  created by the agent's own tool calls);
* root-cause categories must exist in the taxonomy;
* root causes cannot be smuggled in as findings;
* recommended runbooks must come from the retrieved matches.

Rejected outputs are stored with reasons on the `AgentDecision`. If no grounded root cause
remains, the incident records why evidence is insufficient. Text is sanitised and length-capped.
`Finding.evidence_ids` must be non-empty at model level, so this holds for every reasoner.

## Consequences

* Hallucinated facts cannot become findings. Model quality affects the completeness of a
  diagnosis but not its integrity.
