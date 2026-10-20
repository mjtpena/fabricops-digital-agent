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

- Delivery pipeline (ADR-0016): `cd.yml` builds the image once, scans it with Trivy, generates an SPDX SBOM, pushes
  it to the shared registry by digest, tags it with the git SHA only after the scan gate passes, deploys `dev` and
  smoke-tests it, then promotes the same digest to `test` and `prod` through GitHub environments with required
  reviewers. Azure access is OIDC only.
- `whatif.yml`: read-only Bicep what-if on pull requests, uploaded as a values-free summary.
- `release.yml`: version-tag releases with notes from this file, the SBOM, the scan report and the image digest.
- `infra/bicep/env/{dev,test,prod}.bicepparam` (production writes disabled) and `scripts/validate-bicep.sh`.
- `scripts/smoke-test.sh`: `/health` is 200, anonymous `GET /incidents` is 401, and `GET /incidents` with the
  Automation app-role token is 200.
- `containerRegistryResourceId` Bicep parameter and `modules/acr-pull.bicep`: `AcrPull` for the reader identity on a
  registry in another resource group, such as the shared registry.
- `docs/delivery.md`: owner instructions for OIDC federated credentials, least-privilege role assignments, GitHub
  environments and variables.
- `scripts/pip_audit_gate.py` and `.pip-audit-allowlist.toml`: dependency audit with justified, owned, expiring
  exceptions. `.trivyignore.yaml` for image scan exceptions.

### Changed

- CI pins runners to `ubuntu-24.04` and every third-party action by commit SHA, lints workflows with actionlint, audits
  dependencies with pip-audit, scans the image with Trivy and runs weekly.
- The runtime image no longer contains `pip` (its vendored libraries carried fixable HIGH findings).
