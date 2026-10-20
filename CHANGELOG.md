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
- Delivery pipeline (ADR-0016): `cd.yml` runs CI first, then builds the image once, scans it with Trivy, generates an SPDX
  SBOM, pushes it to the shared registry by digest, tags it (`<git sha>` for `main`, `dryrun-<git sha>` otherwise) only
  after the scan gate passes, deploys `dev` and smoke-tests it, then promotes the same digest to `test` and `prod`
  through GitHub environments with required reviewers. Azure access is OIDC only, with separate build, per-environment
  deploy and what-if identities whose federated-credential subjects use the repository's immutable-subject prefix.
- `whatif.yml`: read-only Bicep what-if on pull requests, uploaded as a values-free summary.
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

### Changed

- CI pins runners to `ubuntu-24.04` and every third-party action by commit SHA, lints workflows with actionlint, audits
  dependencies with pip-audit, scans the image with Trivy (any HIGH/CRITICAL fails), runs weekly, and is a reusable
  workflow that CD calls before it builds.
- The container image is based on `mcr.microsoft.com/azurelinux/base/python:3.12` (pinned by digest) instead of Debian
  `python:3.12-slim`: no HIGH or CRITICAL findings, no Docker Hub pull limit. `pip` is removed from the runtime image and
  the build fails if it is still present.
