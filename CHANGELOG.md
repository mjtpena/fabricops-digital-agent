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

- Durable storage and a stateless app (ADR-0017, implements ADR-0008): PostgreSQL adapters (asyncpg, Entra token as the
  password, no password setting) for the event store, incidents, dependencies, approvals (compare-and-set), the audit
  chain (chain-head row lock), the leader lease (database clock, fencing generation) and investigation jobs
  (`FOR UPDATE SKIP LOCKED`, heartbeats, re-claim after a crash); versioned SQL migrations behind an advisory lock;
  Blob evidence/report and audit-anchor adapters (write-once, version-level immutability, OneLake by URL).
  `ORQENTIS_STORAGE_BACKEND=postgres` is required in `dev`/`test`/`prod`; `local` stays the default for development,
  tests, mock mode and `make demo`.
- Fenced writes: scheduled jobs run under the lease generation and every PostgreSQL write transaction checks it, so a
  deposed leader's late write is refused. Detection runs under a cross-replica mutex.
- Audit anchors: the chain head is written to an immutable blob every 15 minutes and `orqentis audit` verifies against
  the latest one; `orqentis audit start-epoch` records a restore. `orqentis db migrate|status|reseed-counters`.
- Bicep: PostgreSQL Flexible Server (B1ms default, Entra-only, PITR), evidence storage account (shared key disabled,
  versioning, version-level immutability), container-scoped role assignments, `minReplicas`/`maxReplicas`
  (1 in dev, 2 in test and prod). `docs/operations/restore.md`, `scripts/postgres-bootstrap.sql`.
- Contract tests for every storage port against the local and the PostgreSQL/Blob adapters, and concurrency tests with
  two instances, two OS processes, a killed process and a graceful stop; CI runs them with PostgreSQL and Azurite.

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
