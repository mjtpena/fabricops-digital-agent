# ADR-0012: LLM hosting for the Agent Framework reasoner

* Status: Accepted (programme decision D7)
* Date: 2026-10-20

## Context

The Agent Framework reasoner (`agents/agent_framework.py`) has only been tested with a scripted
chat client. Production needs a real model endpoint, authenticated with Entra ID (no API keys),
evaluated against a golden set, with the rule-based reasoner as fallback.

## Decision

* A **dedicated Azure OpenAI (Azure AI Services) account** in Australia East, owned by the
  deployment, with local (key) authentication disabled. The reader identity gets
  *Cognitive Services OpenAI User* on that account only.
* Default deployment: **`gpt-5.5`, GlobalStandard**, the most capable model with quota in-region at
  decision time. The WS10 evaluation compares newer deployments (`gpt-5.6-*`) on the golden set
  and changes the default only on evidence (accuracy, grounding rejections, latency, cost).
* **Data residency:** GlobalStandard may process prompts outside Australia. That is accepted for
  the sandbox. Customers who need in-region processing use a DataZone or regional deployment where
  the model offers one; the deployment type is a parameter.
* The rule-based reasoner stays the default in `make demo`, tests and any deployment without an
  endpoint, and is the fallback whenever the model call fails or returns invalid output.

## Consequences

* An optional Bicep module creates the account, the deployment and the role assignment.
* Evaluation runs nightly over OIDC, not on every PR, with a regression threshold.
* Fabric metadata sent to the model passes the existing sanitisation, untrusted-data envelope and
  redaction.
