# Common brief text (appended to every workstream brief)

## Product context

Orqentis FabricOps is an AI reliability engineer for Microsoft Fabric (Python 3.12+, FastAPI,
Pydantic v2, Microsoft Agent Framework, Bicep on Azure Container Apps). Deterministic collectors and
rules detect incidents. Typed read-only tools collect evidence. An investigator agent reasons over
the evidence only. A deterministic confidence model scores the result. Version-controlled runbooks
drive recommendations. A policy and human-approval boundary gates every mutation, and a hash-chained
audit trail records everything. It is a modular monolith built on ports and adapters; the
composition root is `src/orqentis/bootstrap.py`.

Read first: `README.md`, `docs/architecture.md`, `docs/security.md`, `docs/threat-model.md`,
`docs/development.md`, `docs/adr/*`, `infra/bicep/README.md`, and the programme ledger
`docs/production-readiness.md`.

## Invariants (must hold; reviewers will check every one)

1. **The LLM never monitors.** Incidents are opened only by deterministic `DetectionRule`s.
2. **Findings must cite existing evidence ids.** `GroundingValidator` stays mandatory for every
   reasoner.
3. **Confidence is computed in code** (`ConfidenceCalculator`). Never ask the model for a number.
4. **No mutation is ever auto-allowed.** Keep the code-level invariant in the policy engine.
   Unknown action types are HIGH_RISK, and the default effect is DENY.
5. **Read and write are separate ports.** `FabricClient` has no mutation methods. Only the
   `ActionGateway` (and the future executor) holds a `FabricWriteClient`.
6. **Only humans approve and execute.** The agent principal cannot approve or execute, and the
   requester cannot approve their own request.
7. **Execution re-checks everything:** approval state, expiry, action fingerprint, role, single
   use, current policy and write capability.
8. **Fabric metadata is untrusted.** Keep sanitisation, the untrusted-data envelope and redaction
   before logs, evidence, audit and model calls.
9. **No credentials in the repo or in CI variables.** Use Entra ID everywhere: managed identity,
   workload identity or OIDC. No API keys for the LLM.
10. **Ports and adapters.** Azure, Fabric, LLM and storage SDKs are imported only in adapters and
    `bootstrap.py`. Mock mode and `make demo` keep working offline.
11. **Code standards.** `mypy --strict`, ruff and Pydantic models with `extra="forbid"`. Every
    architecture change gets an ADR in `docs/adr/`. Docs, `.env.example`, `settings.py` and Bicep
    parameters stay consistent.
12. **Never weaken tests to get green.** Do not skip, disable or quarantine tests, and do not
    loosen assertions.

## Hot files

These files are shared with workstreams running in parallel: `src/orqentis/bootstrap.py`,
`src/orqentis/config/settings.py`, `.env.example`, `infra/bicep/main.bicep`, `README.md`,
`docs/architecture.md`, `docs/security.md`. Change them **additively** (add settings, parameters,
wiring and sections; do not reorder, rename or reformat existing content). Before opening the PR,
merge the latest `origin/main` and resolve conflicts.

## Working environment

* The machine is Windows with Git Bash, GNU make, Docker and `uv`. If Python 3.12 is missing,
  `uv venv .venv --python 3.12 && uv pip install --python .venv/Scripts/python.exe -e ".[dev,llm]"`
  then `touch .venv/.installed`. The Makefile detects the Windows venv layout.
* CI (GitHub Actions) runs on Ubuntu with Python 3.12 and 3.13.
* Do not use any Azure or Fabric credentials, even if the machine has an `az` login. All work is
  verified against mocks, fakes and recorded fixtures.

## Validation before opening the PR

`make check` (ruff, ruff format, mypy --strict, pytest, runbook validation); `make demo` and
`make demo-all` still run offline; `docker build .` succeeds and `/health` answers; `az bicep build
--file infra/bicep/main.bicep` and `az bicep build-params` for every `.bicepparam` (if infra was
touched); `pytest -m security` passes; any new tests pass.

## Deliverables

Code + tests + docs (+ an ADR if architectural) + `.env.example` / `settings.py` / Bicep parameters
kept consistent.

## Never

Commit secrets or tenant data; use live tenant credentials; weaken or skip tests; change the
approval or policy invariants; widen scope. If you are blocked, or an acceptance criterion is
wrong, stop and write the question in the PR.

## Final report (the PR description)

Summary; how each acceptance criterion is met (with the commands run and their output); deviations;
open questions; follow-ups. End the PR description with:
`🤖 Generated with [Claude Code](https://claude.com/claude-code)`. End commit messages with
`Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.
