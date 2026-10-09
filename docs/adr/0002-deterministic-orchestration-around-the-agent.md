# ADR-0002: Deterministic orchestration around a Microsoft Agent Framework agent

* Status: Accepted (Milestone 5)
* Date: 2026-10-09

## Context

The specification separates detection, evidence, reasoning, decision and action, and requires
deterministic orchestration around the agent. Letting an LLM drive the whole investigation would
make the evidence collected, the cost and the outcome non-reproducible and hard to audit.

## Decision

* The 20-step pipeline-failure investigation is plain async Python
  (`incidents/workflows/pipeline_failure.py`). It calls the same typed tool registry the agent
  uses, so every fact is collected deterministically, logged and recorded as evidence.
* The agent (`FabricOpsInvestigator`) is invoked once, at step 14, with the evidence package and
  the platform's deterministic analysis. It may make additional **read-only** tool calls (with an
  allowlist and a budget) and must return a JSON `AgentDiagnosis`.
* The LLM backend is Microsoft Agent Framework (`agent_framework.Agent` with `FunctionTool`s whose
  schemas are the tools' Pydantic input models). A `RuleBasedReasoner` implements the same
  `Reasoner` port. It is the default for the offline demo and tests, and the fallback when the LLM
  fails or returns invalid output.
* Agent Framework workflows (`WorkflowBuilder`) are not used for orchestration in the MVP. A plain
  coroutine is simpler to read, test and trace for a linear investigation. This can be revisited
  for long-running, checkpointed or human-in-the-loop flows (for example Teams approvals).

## Consequences

* Investigations are reproducible, and the agent's contribution is isolated and auditable
  (`AgentDecision`).
* The deterministic reasoner encodes diagnostic heuristics per category. These must evolve with
  the runbooks; the LLM path generalises beyond them.
