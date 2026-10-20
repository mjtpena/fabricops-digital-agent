# Changelog

All notable changes to Orqentis FabricOps are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Cutting a release: move the entries under *Unreleased* into a new `## [X.Y.Z] - YYYY-MM-DD` section, bump `version` in
`pyproject.toml` to match, merge to `main`, let CD finish, then push the tag `vX.Y.Z`
([docs/delivery.md](docs/delivery.md), *Releasing*). The release workflow refuses a tag whose version or changelog
section does not match.

## [Unreleased]

### Added

- Observability and operations (ADR-0019): OpenTelemetry metrics behind `observability/metrics.py` (collection duration and
  errors per collector, events ingested, incidents opened and detection latency, investigation duration and outcome, tool errors,
  model tokens and estimated cost, pending approvals, scheduler job runs/failures/overruns, lease changes, and a collection
  heartbeat that moves only for a collection in which not everything failed); the scheduled `audit-verify` job (metric plus an
  ERROR log line on failure, optional immutable-anchor check); `infra/bicep/modules/monitoring.bicep` with an action group (email
  receivers and an optional Teams Workflows webhook, both deployment-time inputs), six alerts (app down, restart loop, 5xx rate,
  collector errors, collection heartbeat, audit verification failure) with parameterised windows and thresholds and runbook links,
  and a workbook; `docs/operations.md` and `docs/cost.md`. New settings `ORQENTIS_SCHEDULER_AUDIT_VERIFY_*` and
  `ORQENTIS_LLM_*_COST_PER_MILLION_TOKENS`; Bicep parameters `monitoringEnabled`, `alert*`, `auditVerify*`, `logDailyQuotaGb`,
  `llm*CostPerMillionTokens`. Nothing is exported unless Application Insights or the console exporter is configured.
- Notifications and a web UI (ADR-0018, implements ADR-0010). A `Notifier` port with a Microsoft Teams Workflows webhook adapter (an
  Adaptive Card with a link and no buttons), a generic JSON webhook adapter (optional HMAC-SHA256 signature) and a null adapter; incident
  opened / escalated, approval requested and action executed / failed are announced with deep links. Delivery is in the background:
  de-duplicated by event id, a bounded outbox, retries with back-off and jitter, one audit record per attempt, never blocking detection or
  an investigation. Content is sanitised, redacted and length-limited. Webhook URLs are Key Vault references; every call passes an SSRF
  guard (https, host allow list, public addresses after DNS, connection pinned to the checked address, no redirects).
- A server-rendered UI under `/ui` (Jinja2, no script): incident list with filters, incident detail, the report with FACT / INFERENCE /
  RECOMMENDATION labels, approvals with approve / reject / execute and a comment, audit verification. Same guards and services as the API;
  CSRF form tokens, strict Content-Security-Policy and security headers.
- `orqentis notify-test`, `docs/teams-notifications.md`, and Bicep parameters `notifierKind`, `notify*SecretUri`, `notifyAllowedHosts`,
  `publicBaseUrl`, `uiEnabled`, `uiSignInClientSecretUri` (interactive Easy Auth sign-in with a Key Vault client secret) and `uiCsrfSecretUri`.
- Delivery pipeline (ADR-0016): `cd.yml` runs CI first, then builds the image once, scans it with Trivy, generates an SPDX
  SBOM, pushes it to the shared registry by digest, tags it (`<git sha>` for `main`, `dryrun-<git sha>` otherwise) only
  after the scan gate passes, deploys `dev` and smoke-tests it, then promotes the same digest to `test` and `prod`
  through GitHub environments with required reviewers. Azure access is OIDC only, with separate build, per-environment
  and deploy identities whose federated-credential subjects use the repository's immutable-subject prefix.
- `bicep-diff.yml`: an offline diff of what each environment would deploy on pull requests (`bicep snapshot` of the base
  and the merged pull request, no Azure access, no credentials), and a recorded `az deployment group what-if` in each
  deploy job, run with the environment's own deploy identity before it deploys.
- `release.yml`: version-tag releases in a dedicated `release` environment, with notes from this file, the SBOM, the scan
  report and the image digest of the successful CD run of that commit on `main`.
- `infra/bicep/env/{dev,test,prod}.bicepparam` (production writes disabled) and `scripts/validate-bicep.sh`.
- `scripts/smoke-test.sh`: the latest revision is ready, serves all traffic and runs the deployed digest; `/health` is
  200; anonymous `GET /incidents` is 401; `GET /incidents` with the Automation app-role token is 200.
- `containerRegistryResourceId` Bicep parameter and `modules/acr-pull.bicep`: `AcrPull` for the reader identity on a
  registry in another resource group, such as the shared registry.
- `docs/delivery.md`: owner instructions for OIDC federated credentials, least-privilege role assignments (including the
  constrained RBAC Administrator and the nested-deployment role), GitHub environments, variables, and the rulesets that
  protect `main` and the release tags.
- `scripts/pip_audit_gate.py` and `.pip-audit-allowlist.toml`: dependency audit with justified, owned, expiring
  exceptions. `.trivyignore.yaml` for image scan exceptions. `scripts/release_tools.py` for release notes and evidence.

### Security

- Every state-changing request, API and UI, passes a cross-origin guard (`Origin`, `Sec-Fetch-Site`; `Referer` on `/ui`), and the API accepts
  JSON bodies only (415 otherwise). An ID-token browser session is a person, so without this a foreign page could
  `POST /approvals/{id}/approve` (ledger R6). Reads that spend a rate budget (`/ui/*`, `/audit/verify`) also refuse cross-site
  subresource loads, so a foreign page cannot use up a person's budget.
- Settings validation errors no longer print the raw environment (`hide_input_in_errors`), which showed the end of the webhook
  signature, signing key and CSRF key in a start-up traceback. Notification text is redacted before it is shortened; the HTTP
  client's loggers are pinned to WARNING; IPv6 site-local and `192.88.99.0/24` are refused as webhook destinations.

### Changed

- CI pins runners to `ubuntu-24.04` and every third-party action by commit SHA, lints workflows with actionlint, audits
  dependencies with pip-audit, scans the image with Trivy (any HIGH/CRITICAL fails), runs weekly, and is a reusable
  workflow that CD calls before it builds.
- The container image is based on `mcr.microsoft.com/azurelinux/base/python:3.12` (pinned by digest) instead of Debian
  `python:3.12-slim`: no HIGH or CRITICAL findings, no Docker Hub pull limit. `pip` is removed from the runtime image and
  the build fails if it is still present.
