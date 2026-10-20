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
advisories appear without code changes. CI is also a reusable workflow (`workflow_call`) with separate concurrency
groups per event, so the weekly run, pushes and pull requests never cancel one another and a run on `main` is never
cancelled.

**CD (`cd.yml`).** Pushes to `main` do not trigger CI on their own; `cd.yml` calls it as its first job and the `build`
job `needs` it, so nothing is built, pushed or deployed from `main` unless lint, types, tests, the dependency audit and
the image scan passed, whatever the branch protection says. One job then builds the image **once** with Buildx and
provenance (`mode=max`), pushes it to the shared registry **by digest with no tag**, generates an SPDX SBOM, scans that
digest, and only if the scan gate passes adds a tag. The tag is `<git sha>` for a build of `main` and `dryrun-<git sha>`
for everything else, and a tag is never moved, so `<git sha>` proves an image from `main` passed the gate and a branch
image can never carry it. The digest, not a tag, identifies the release from then on: `deploy-dev`, `promote-test` and
`promote-prod` all deploy `needs.build.outputs.image` (`<registry>/orqentis-fabricops@sha256:...`) through one reusable
workflow (`deploy-environment.yml`) that deploys with `az deployment group create` and
`infra/bicep/env/<env>.bicepparam` and then runs `scripts/smoke-test.sh`. The `test` and `prod` GitHub environments
hold required reviewers, so the jobs wait for approval. The build job declares no environment; its OIDC subject is the
triggering ref. A push to a `cd-dryrun/**` branch (or a manual run with "deploy" unticked) is a build/scan/push dry run
that skips every deployment. The dedicated push trigger exists because GitHub only dispatches `workflow_dispatch` for a
workflow file that is already on the default branch, so a new workflow could not otherwise be exercised before it
merges; it is inert until a federated credential is registered for the branch.

**Scan policy.** The gate fails on **any HIGH or CRITICAL finding, fixed or not**, in CI and in CD. Findings are
accepted only in `.trivyignore.yaml`, which Trivy stops honouring after `expired_at` (at most 60 days ahead, owner and
reason in the statement), so an exception cannot outlive its review and the owner decides each one. A weekly rescan and
Dependabot's base-image digest updates pick up fixes.

**Base image.** The runtime moved from Debian `python:3.12-slim` (via the ECR Public mirror) to Microsoft's Azure Linux
3 `mcr.microsoft.com/azurelinux/base/python:3.12`, pinned by digest. The Debian image carried eight HIGH CVEs with no fix
available (util-linux, ncurses, libacl1, libsystemd, perl), which under a strict gate would have needed eight standing
exceptions; the Azure Linux image has none, fixed or unfixed, and MCR has no anonymous pull limit. The application user is
added to `/etc/passwd` directly (the image has no `useradd`) with the same UID/GID 10001 the Azure Files mount options
expect. The runtime stage removes pip (the venv's copy with `python -m pip uninstall`, the base image's RPMs with `rpm
-e`) and the build fails if `pip` can still be run: pip's vendored libraries were the only fixable findings in the
Debian image, and the app never installs anything at run time. Dependabot is told to hold the Python 3.12 tag and only
refresh the digest.

**Identities (OIDC only, no secrets).** `azure/login` takes client, tenant and subscription ids from GitHub variables
(identifiers, not secrets). Federated-credential subjects start with the repository's immutable-subject prefix
(`sub_claim_prefix`, which embeds the numeric owner and repository ids), read from the GitHub API rather than typed, and the
`environment:*` credentials are created only after the environments' reviewer and branch rules have been verified as enforced. Three kinds of Entra identity are never shared. The **build** identity (the existing
`fabricops-cicd-dev`) holds `AcrPush` on the registry and nothing else; subjects `ref:refs/heads/main`, the dry-run branch
and `environment:release`. A **deploy** identity per environment (`fabricops-deploy-dev|test|prod`, subject
`environment:<env>`) holds `Contributor` on its own resource group, `Role Based Access Control Administrator` constrained
by an ABAC condition to assigning `AcrPull` and `Cognitive Services OpenAI User` to service principals only and unable
to delete any assignment, and a custom role with only `Microsoft.Resources/deployments/*` on the shared resource group
(the registry and OpenAI role modules are deployed there as nested deployments). No environment identity can touch another
environment, and no identity that pushes images can deploy. Pull requests use a fourth identity (`fabricops-whatif`,
subject `pull_request`) that can only simulate deployments of `dev`. The smoke-test identity is the environment's deploy
service principal holding the `FabricOps.Automation` app role on that environment's API registration (WS3); the token comes
from `az account get-access-token`, so no secret exists anywhere.

**Protection of `main` and releases.** Rulesets (docs/delivery.md, section 8) require a pull request and all six CI jobs
to pass on `main` with no bypass, forbid force pushes and deletion, and restrict creating `v*` tags to repository admins.
Releases that need Azure run only in a dedicated `release` GitHub environment (required reviewers, tag rule `v*`, its own
OIDC subject), never in `dev`.

**Smoke test.** Before any HTTP call the script reads the container app and its latest revision and requires that the
latest revision is the latest ready one, serves 100% of traffic and runs exactly the deployed digest. Without that check a
failed rollout hides behind the previous revision still answering. Then `/health` (anonymous: the deployment excludes it
from authentication), an anonymous `/incidents` that must be 401, and an authenticated `/incidents` that must be 200.

**What-if on pull requests (`whatif.yml`).** Read-only `az deployment group what-if` against `dev`, skipped for forks,
when unconfigured, and when the resource group does not exist. Its identity has rights on the dev resource group only;
the cross-resource-group `AcrPull` module is left out of the simulation (empty `containerRegistryResourceId`) so that a
pull-request identity needs nothing on the shared registry's resource group. The uploaded summary lists changed resources
and property paths only. Values are omitted on purpose: the template reads storage and workspace keys with `listKeys()`,
and what-if payloads can contain them.

**Releases (`release.yml`).** A `vMAJOR.MINOR.PATCH` tag on a commit that is on `main` must match the package version
and have a non-empty `CHANGELOG.md` section. The workflow does not rebuild: it takes the digest and SBOM from the
**successful** CD run on `main` for that commit (so the commit went through every stage, including the `prod` approval),
verifies the registry holds that digest under the commit tag (so the scan gate passed), adds the semver tag (never moving
an existing one) after the `release` environment's approval, and creates the GitHub release with the notes, the SBOM, the
scan report and the digest. The artifact handling lives in `scripts/release_tools.py` with unit tests: the GitHub CLI
puts several artifacts from one `gh run download` into sub-directories, so each is fetched with its own call.

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
* The pipeline holds no secret; compromise of a workflow run yields at most a short-lived token for one environment, and
  a token that can push images cannot deploy.
* CI runs twice for a merge (on the pull request, then again inside `cd.yml`). That is the price of making the registry
  and the environments unreachable without CI, independent of branch protection.
* The smoke tests need the API reachable from a GitHub-hosted runner: the environment files set
  `ingressMode = 'externalAuthenticated'` (ADR-0015; Entra-authenticated, unauthenticated callers get 401) with no IP
  allow list, and keep `anonymousHealthEndpoints = true` so that `/health` needs no token, which is what the smoke test
  asserts. CI (`scripts/validate-bicep.sh`) fails if an environment file reverts to the deprecated `externalIngress`, drops
  the anonymous health endpoints or turns the scheduler off. Whether `prod` should be public at all is the owner's call;
  `vnet` mode needs a runner inside the network. If a deployment turns the anonymous endpoints off, set
  `SMOKE_HEALTH_AUTHENTICATED=true`.
* The autonomous scheduler (ADR-0013) is on in all three environments. A rollout runs the old and the new revision side
  by side on one SQLite volume until the PostgreSQL system of record exists, so the start-up delay and a post-deploy
  `GET /audit/verify` are the mitigations (infra/bicep/README.md); a pipeline step that verifies the audit chain is a follow-up.
* Required reviewers, deployment rules and rulesets on a private repository need a GitHub plan that supports them; the
  owner must verify that before `prod` is configured (docs/delivery.md).
* The dependency set is not locked (`pip install` resolves fresh), so image builds are not bit-for-bit reproducible.
  A lock file with hashes is a follow-up; the SBOM records what each build resolved.
* Alerts and dashboards (WS9) and the executor deployment (WS11) are out of scope.
