# Workstream briefs

Each sub-session receives its workstream brief followed by [`_common.md`](_common.md) (product
context, invariants, hot-file rules, validation, the never-list and the report format). Briefs are
standalone: sub-sessions see nothing else from the programme conversation.

| WS | Brief | Wave |
|---|---|---|
| WS1 | [Autonomous runtime](WS1-autonomous-runtime.md) | 1 |
| WS2 | [Portability and contracts](WS2-portability-contracts.md) | 1 |
| WS3 | [Identity and API hardening](WS3-identity-api-hardening.md) | 1 |
| WS4 | [Delivery pipeline](WS4-delivery-pipeline.md) | 1 |
| WS5 | [Live sandbox estate and live test harness](WS5-live-sandbox.md) (orchestrator-run) | 1 |

Merge order in Wave 1 (hot-file conflicts): WS0 → WS3 → WS2 → WS1 → WS4, adjusted to readiness;
after each merge, `main` is merged into the open branches that share hot files.
