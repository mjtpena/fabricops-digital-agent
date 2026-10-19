You are implementing **WS4: Delivery pipeline** in `mjtpena/fabricops-digital-agent`.
Base: latest `origin/main`. Branch: `prod/ws4-delivery-pipeline`. Open one PR to `main` when done; never push to `main`.

## Context

`.github/workflows/ci.yml` runs ruff, mypy, pytest, runbook validation and the offline demo on
Python 3.12/3.13, builds the Bicep template, builds the Docker image (no push) and smoke-tests
`/health`. The base image is pulled from the ECR Public mirror of the Docker official image and
pinned by digest; Dependabot updates the digest and action versions (`.github/dependabot.yml`).
There is no CD, no environment parameter files, no image or dependency scanning, no SBOM and no
release process. Deployment today is manual (`infra/bicep/README.md`: `az acr build`,
`az deployment group create` with `infra/bicep/main.bicep` and `main.bicepparam`).

Target topology (decision D4, pending owner confirmation; design for it but keep names in
parameters): one Azure subscription, resource groups `rg-fabricops-dev`, `rg-fabricops-test`,
`rg-fabricops-prod` in Australia East, plus `rg-fabricops-shared` holding one Azure Container
Registry. Images are built once and promoted between environments **by digest**. GitHub
environments `dev`, `test`, `prod`; `prod` (and `test`) need required reviewers. Azure access from
GitHub Actions uses **OIDC federated credentials only** (`azure/login` with `client-id`,
`tenant-id`, `subscription-id` from environment *variables*, which are identifiers, not secrets).
Prefer a separate Entra identity per environment so that the `dev` pipeline cannot touch `prod`.

`ubuntu-latest` began migrating to Ubuntu 26 on 2026-10-19; pin runner images.

Parallel workstreams: WS1 adds `/health/ready` and scheduler settings; WS2 adds Bicep parameters
(time zone, service-principal mode); WS3 adds the `FabricOps.Automation` app role and ingress
modes. Use only parameters that exist on `main` in the parameter files, and leave clearly marked
placeholders for values the owner supplies.

## Goal

A secure, reproducible delivery pipeline: every merge to `main` builds one image, scans it,
produces an SBOM, pushes it to ACR, deploys it to `dev` and smoke-tests it; promotion to `test`
and `prod` reuses the same digest and requires approval. PRs show a Bicep what-if. Releases are
tagged with a CHANGELOG. The repo and pipeline hold no secrets.

## In scope

1. **CI hardening** (`ci.yml`): pin runner images (`ubuntu-24.04`); pin every third-party action by
   full commit SHA (with the version in a comment); add `actionlint`; add `pip-audit` against the
   resolved dependency set (fail on vulnerabilities unless listed in an allowlist file with a
   justification, an owner and an expiry date); keep every existing step.
2. **CD workflow** (`cd.yml`), triggered on push to `main` and manually:
   * build the image once (Buildx, provenance on), scan it with Trivy or Grype and **fail on
     unaccepted HIGH/CRITICAL** (accepted findings live in a reviewed ignore file with
     justification and expiry);
   * generate an SBOM (SPDX or CycloneDX) and upload it as a workflow artifact (and attach it to
     releases);
   * push to ACR tagged with the full git SHA (and semver on release), record the digest;
   * deploy to `dev` with `az deployment group create` using `infra/bicep/env/dev.bicepparam`
     and the image **digest**, then run the smoke test;
   * promote to `test`, then `prod`, through GitHub environments with required reviewers, deploying
     the same digest and running the smoke test after each.
3. **What-if on PRs**: a job that logs in over OIDC (pull-request subject) and runs
   `az deployment group what-if` for `dev` (and optionally `prod`), uploading the result as an
   artifact. It must not deploy anything. Document the minimum role it needs.
4. **Per-environment parameter files**: `infra/bicep/env/{dev,test,prod}.bicepparam` with
   `readEnvironmentVariable()` for values supplied by the pipeline (image reference, client ids).
   `prod` keeps `enableWriteActions = false`. Every file must pass `az bicep build-params`.
5. **Post-deploy smoke test** script (for example `scripts/smoke-test.sh`): `/health` returns 200;
   an unauthenticated `GET /incidents` returns 401; an authenticated `GET /incidents` with a token
   for the API app, obtained by the pipeline's identity holding the `FabricOps.Automation` app role
   (WS3), returns 200. If WS3 has not merged, implement it against the documented role and say so.
6. **Release workflow** (`release.yml`): on a `v*.*.*` tag, verify the tag matches the package
   version, build release notes from `CHANGELOG.md`, attach the SBOM and the image digest. Create
   `CHANGELOG.md` (Keep a Changelog format) with an `Unreleased` section.
7. **Owner instructions** (`docs/delivery.md`): exact, copy-pasteable steps for everything that
   needs repo-admin or tenant-admin rights:
   * the Entra app registration(s) and federated credential subjects
     (`repo:mjtpena/fabricops-digital-agent:environment:<env>`, `...:pull_request`,
     `...:ref:refs/heads/main`);
   * the least-privilege Azure role assignments for each identity (AcrPush on the registry; on each
     environment RG, deployment rights plus a **constrained** RBAC Administrator role that can only
     assign the roles the template assigns, such as AcrPull and Cognitive Services OpenAI User);
   * GitHub environments, required reviewers, deployment branch rules (`main` only), and the
     environment variables to set;
   * the app-role assignment that lets the smoke-test identity call the API.
8. Update `infra/bicep/README.md` and `docs/development.md` to point at the pipeline.

## Out of scope

Creating any Azure or Entra resource yourself (the orchestrator does it after owner approval).
Application code changes. Alerts and dashboards (WS9). Executor deployment (WS11).

## Likely files

`.github/workflows/ci.yml`, `.github/workflows/cd.yml` (new), `.github/workflows/release.yml`
(new), `infra/bicep/env/*.bicepparam` (new), `scripts/smoke-test.sh` (new),
`.trivyignore` / `pip-audit` allowlist (new, empty or justified), `CHANGELOG.md` (new),
`docs/delivery.md` (new), `infra/bicep/README.md`, `docs/development.md`. Hot files: avoid
`infra/bicep/main.bicep` unless strictly needed (additive only); `README.md` (additive).

## Acceptance criteria

1. `actionlint` passes on every workflow (show the output).
2. Every third-party action is pinned by commit SHA; runners are pinned.
3. `pip-audit` and the image scan run in CI, fail on unaccepted HIGH/CRITICAL, and the current
   image and dependencies pass (or each finding is listed with justification for the owner to
   accept).
4. All three `.bicepparam` files pass `az bicep build-params`; `prod` has writes disabled.
5. The CD workflow builds once and promotes by digest; `prod` deploys only through an environment
   with required reviewers; no workflow references a secret (`secrets.*` other than the default
   `GITHUB_TOKEN`), and no credential appears in any variable.
6. `docs/delivery.md` lets the owner configure OIDC, environments and variables without guessing.
7. Dry run (completed by the orchestrator after the owner configures OIDC): a manual run on the
   branch builds, scans and pushes to the dev ACR. Leave the workflow runnable via
   `workflow_dispatch` on a branch, with deployment steps skippable for that dry run.
8. `make check` and `docker build` still pass.

Note: GitHub Actions may be unable to start jobs until the owner resolves a billing issue on the
account. If CI cannot run, validate locally (actionlint, `az bicep build-params`, `docker build`,
Trivy and `pip-audit` locally if available) and say so in the PR.
