# ADR-0016: Delivery pipeline: build once, scan, promote by digest

* Status: Accepted (implements ADR-0011; programme workstream WS4)
* Date: 2026-10-20

## Context

ADR-0011 fixes the topology: one subscription, resource groups `rg-fabricops-dev`, `-test`, `-prod` and
`rg-fabricops-shared` with one Azure Container Registry, OIDC only, a separate Entra identity per environment.
Until now CI only built and tested; deployment was a manual `az acr build` and `az deployment group create`.
There was no image or dependency scanning, no SBOM, no environment parameters and no release process, and the
workflows used floating action tags and `ubuntu-latest` (which began moving to Ubuntu 26 on 2026-10-19).

## Decision

**CI hardening (`ci.yml`).** Runners are pinned to `ubuntu-24.04`. Every third-party action is pinned by full commit
SHA with the version in a comment; Dependabot updates both. `actionlint` (pinned release, checksum verified) lints the
workflows. `pip-audit` audits the dependency set the image ships (`.[llm,azure-monitor]`, resolved fresh) through
`scripts/pip_audit_gate.py`, which fails on any vulnerability not in `.pip-audit-allowlist.toml`; pip-audit has no
expiry of its own, so the gate requires a justification, an owner and an expiry (at most 90 days ahead) for each entry
and treats an expired entry as no entry. The image built in CI is scanned with Trivy. CI also runs weekly, because
advisories appear without code changes.

**CD (`cd.yml`).** On every push to `main` (and manually) one job builds the image **once** with Buildx and provenance
(`mode=max`), pushes it to the shared registry **by digest with no tag**, generates an SPDX SBOM, scans that digest,
and only if the scan gate passes adds the tag `<git sha>`. A tag therefore proves the image passed. The digest, not a
tag, identifies the release from then on: `deploy-dev`, `promote-test` and `promote-prod` all deploy
`needs.build.outputs.image` (`<registry>/orqentis-fabricops@sha256:...`) through one reusable workflow
(`deploy-environment.yml`) that deploys with `az deployment group create` and `infra/bicep/env/<env>.bicepparam`
and then runs `scripts/smoke-test.sh`. The `test` and `prod` GitHub environments hold required reviewers, so the
jobs wait for approval. The build job declares no environment; its OIDC subject is the triggering ref. A push to a
`cd-dryrun/**` branch (or a manual run with "deploy" unticked) is a build/scan/push dry run that skips every
deployment. The dedicated push trigger exists because GitHub only dispatches `workflow_dispatch` for a workflow file
that is already on the default branch, so a new workflow could not otherwise be exercised before it merges; it is inert
until a federated credential is registered for the branch.

**Scan policy.** The gate fails on **HIGH and CRITICAL findings that have a fix available**. Findings the vendor has
not fixed (Debian marks them `affected` or `fix_deferred`) cannot be acted on and arrive continuously, so they would
block every release; they are reported in full in the job summary and the `trivy-report` artifact but do not fail
the build. Accepted findings live in `.trivyignore.yaml`, which Trivy stops honouring after `expired_at`, so an
exception cannot outlive its review. A weekly rescan and Dependabot's base-image digest updates pick up fixes. The
alternative of failing on unfixed findings too is a one-line change (`ignore-unfixed: false`) if the owner wants it,
at the price of an ignore entry for every unfixed Debian CVE.

**Dockerfile.** The runtime image removes `pip`. The current findings on the pinned base were all libraries vendored
inside pip (urllib3, msgpack, setuptools); the image never installs anything at run time, so removing pip fixes the
findings and removes unneeded attack surface instead of accepting them.

**Identities (OIDC only, no secrets).** `azure/login` takes client, tenant and subscription ids from GitHub variables
(identifiers, not secrets). Per environment there is an Entra identity (`fabricops-cicd-dev|test|prod`) whose
federated credential subject is `repo:<owner>/<repo>:environment:<env>`. The build/push identity is the `dev` one,
which holds `AcrPush` on the registry; its `ref:refs/heads/main` credential serves the build job. Pull requests use a
fourth identity (`fabricops-whatif`, subject `...:pull_request`) that can only simulate deployments. Deployment rights
are `Contributor` on the environment's own resource group, plus `Role Based Access Control Administrator` with an ABAC
condition limiting it to assigning `AcrPull` and `Cognitive Services OpenAI User`. No environment identity can touch
another environment. The smoke test identity is the environment's CI/CD service principal holding the
`FabricOps.Automation` app role on that environment's API registration (WS3); the token comes from
`az account get-access-token`, so no secret exists anywhere.

**What-if on pull requests (`whatif.yml`).** Read-only `az deployment group what-if` against `dev`, skipped for forks,
when unconfigured, and when the resource group does not exist. The uploaded summary lists changed resources and
property paths only. Values are omitted on purpose: the template reads storage and workspace keys with `listKeys()`,
and what-if payloads can contain them.

**Releases (`release.yml`).** A `vMAJOR.MINOR.PATCH` tag on a commit that is on `main` must match the package version
and have a non-empty `CHANGELOG.md` section. The workflow does not rebuild: it takes the digest and SBOM from the CD
run for that commit, verifies the registry holds that digest under the commit tag (so the scan gate passed), adds the
semver tag (never moving an existing one), and creates the GitHub release with the notes, the SBOM, the scan report
and the digest.

**Bicep.** `infra/bicep/env/{dev,test,prod}.bicepparam` read pipeline-supplied values (image, registry, API client
id) with `readEnvironmentVariable()` and no default, so a missing variable fails instead of deploying a wrong value;
`scripts/validate-bicep.sh` compiles all of them with placeholders in CI and fails if `prod` ever enables write
actions. One additive template change was necessary: the existing AcrPull grant only works for a registry in the same
resource group, but the registry is shared. `containerRegistryResourceId` plus `modules/acr-pull.bicep` grant the
reader identity `AcrPull` on a registry in any resource group before the app is created, using the same
module-per-scope pattern as the Azure OpenAI role.

## Consequences

* A merge to `main` yields one scanned, attested image that is what runs in `dev`, `test` and `prod`; rollback is
  deploying an earlier digest.
* The pipeline holds no secret; compromise of a workflow run yields at most a short-lived token for one environment.
* `dev` smoke tests need the API reachable from a GitHub-hosted runner: the environment files set
  `externalIngress = true` (Entra-authenticated; unauthenticated callers get 401). WS3 owns the ingress modes and
  decides whether `/health` is excluded from authentication; until then set `SMOKE_HEALTH_AUTHENTICATED=true`.
* Required reviewers on a private repository need a GitHub plan that supports environment protection rules; the owner
  must verify that before `prod` is configured (docs/delivery.md).
* The dependency set is not locked (`pip install` resolves fresh), so image builds are not bit-for-bit reproducible.
  A lock file with hashes is a follow-up; the SBOM records what each build resolved.
* Alerts and dashboards (WS9) and the executor deployment (WS11) are out of scope.
