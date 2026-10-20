# Delivery

How Orqentis FabricOps gets from a merge to `main` into `dev`, `test` and `prod`, and exactly what an owner or
tenant admin must set up once so the pipeline can run. The design is in
[ADR-0016](adr/0016-delivery-pipeline.md); the topology is in [ADR-0011](adr/0011-deployment-model-and-environments.md).

**Nothing in the repository or in GitHub holds a secret.** Azure access is OIDC (workload identity federation): a
workflow run presents a short-lived GitHub-signed token and Entra ID exchanges it for an Azure token, but only for a
run whose *subject* matches a federated credential you created. Tenant, subscription and client ids are identifiers,
so they live in GitHub *variables*, not secrets.

## The pipeline

```text
pull request ──► ci.yml        ruff, mypy --strict, pytest, runbooks, demo, actionlint, pip-audit, Bicep build,
                               docker build + /health + Trivy scan
             └─► whatif.yml    az deployment group what-if against dev (read-only), summary as an artifact

push to main ──► ci.yml
             └─► cd.yml
                   build ────────────────────────────────────────────────────────────────────────────────────────
                     buildx build ONCE, push <acr>/orqentis-fabricops@sha256:… (no tag), SPDX SBOM, Trivy gate,
                     tag :<git sha> only if the gate passed        artifacts: image-digest, sbom, trivy-report
                   deploy-dev ──► promote-test ──► promote-prod        the SAME digest each time
                     bicep env/<env>.bicepparam, smoke test after each; test and prod wait for required reviewers

tag vX.Y.Z ──► release.yml     verify tag = pyproject version = CHANGELOG section, take digest + SBOM from the CD run
                               for that commit, add :X.Y.Z, create the GitHub release
```

| Workflow | Trigger | OIDC subject it presents | Azure identity |
|---|---|---|---|
| `cd.yml` job `build` | push to `main`; push to `cd-dryrun/**`; manual run | `repo:mjtpena/fabricops-digital-agent:ref:refs/heads/main`; for a dry run `...:ref:refs/heads/cd-dryrun/<name>`; for a manual run on a branch `...:ref:refs/heads/<branch>` | `fabricops-cicd-dev` (`AcrPush`) |
| `cd.yml` → `deploy-environment.yml` (dev) | after `build` | `repo:mjtpena/fabricops-digital-agent:environment:dev` | `fabricops-cicd-dev` |
| same (test) | after dev, approval | `repo:mjtpena/fabricops-digital-agent:environment:test` | `fabricops-cicd-test` |
| same (prod) | after test, approval | `repo:mjtpena/fabricops-digital-agent:environment:prod` | `fabricops-cicd-prod` |
| `whatif.yml` | pull request touching `infra/**` | `repo:mjtpena/fabricops-digital-agent:pull_request` | `fabricops-whatif` |
| `release.yml` job `publish` | tag `v*.*.*` | `repo:mjtpena/fabricops-digital-agent:environment:dev` | `fabricops-cicd-dev` |

The first four rows are the whole deployment path; the `dev` identity cannot touch `test` or `prod`, because each
environment has its own identity with rights on its own resource group only.

## One-time setup (owner and tenant admin)

Everything here needs rights the pipeline deliberately does not have: Entra application admin, Owner or User Access
Administrator on the subscription, and repository admin. Nothing in the pipeline creates any of it. Run the commands
in Git Bash, WSL or any POSIX shell with the Azure CLI (`az login` as yourself) and the GitHub CLI (`gh auth login` as a
repository admin). In **Git Bash on Windows** prefix commands that take an Azure scope (`/subscriptions/...`) with
`MSYS_NO_PATHCONV=1`, or Git Bash rewrites the path.

### 0. Variables used below

```bash
export REPO=mjtpena/fabricops-digital-agent
export TENANT_ID=4b7e9d9f-8e54-43da-a1dd-2d1cc793721a
export SUBSCRIPTION_ID=c8ce2a82-b8b8-4b82-b493-1f82ec33b961
export LOCATION=australiaeast
export ACR_NAME=crfabricopsshared
export ACR_RG=rg-fabricops-shared
export ACR_ID="/subscriptions/$SUBSCRIPTION_ID/resourceGroups/$ACR_RG/providers/Microsoft.ContainerRegistry/registries/$ACR_NAME"
az account set --subscription "$SUBSCRIPTION_ID"
```

For another customer, change these values; nothing else in the repository refers to them.

### 1. Environment resource groups

The pipeline never creates resource groups. Create them once:

```bash
for ENV in dev test prod; do
  az group create --name "rg-fabricops-$ENV" --location "$LOCATION" \
    --tags owner=fabricops-programme purpose="$ENV"
done
```

A deployment to an environment whose resource group is missing stops with a clear error (and the what-if reports
"skipped"). A manual CD run with *deploy* unticked never needs them.

### 2. API app registration per environment

Each environment has its own "Orqentis FabricOps API" app registration, so a token for `dev` is useless against `prod`.
Follow [`infra/bicep/README.md`](../infra/bicep/README.md) section 1 once per environment (display names
`Orqentis FabricOps API (dev)`, `(test)`, `(prod)`) and note each `appId`:

```bash
export DEV_API_APP_ID=<appId of the dev API registration>
export TEST_API_APP_ID=<appId of the test API registration>
export PROD_API_APP_ID=<appId of the prod API registration>
```

The registration needs the **`FabricOps.Automation`** application role (`allowedMemberTypes: ["Application"]`), which
the smoke test identity uses. It is added by the WS3 script in that README. If it is not there yet, add it by hand,
keeping the roles that already exist (`--app-roles` replaces the whole list):

```bash
APP_ID=$DEV_API_APP_ID        # repeat for test and prod
az ad app show --id "$APP_ID" --query appRoles -o json > roles.json
python3 - <<'PY'
import json, uuid
roles = json.load(open("roles.json"))
if not any(r["value"] == "FabricOps.Automation" for r in roles):
    roles.append({"allowedMemberTypes": ["Application"], "isEnabled": True, "id": str(uuid.uuid4()),
                  "value": "FabricOps.Automation", "displayName": "FabricOps Automation",
                  "description": "Automation identities (smoke tests, schedulers): read endpoints and telemetry collection. Never approves or executes."})
json.dump(roles, open("roles.json", "w"))
PY
az ad app update --id "$APP_ID" --app-roles @roles.json
rm roles.json
```

### 3. CI/CD identities (Entra apps with federated credentials, no secrets)

`fabricops-cicd-dev` already exists with credentials `gh-env-dev` (`environment:dev`) and `gh-ref-main`
(`ref:refs/heads/main`) and `AcrPush` on the registry. Create the others. Each app gets one service principal and
**no client secret or certificate**:

```bash
ISSUER=https://token.actions.githubusercontent.com
AUDIENCE=api://AzureADTokenExchange

new_identity() {   # new_identity <display name> <credential name> <subject>
  local app_id
  app_id=$(az ad app create --display-name "$1" --sign-in-audience AzureADMyOrg --query appId -o tsv)
  az ad sp create --id "$app_id" > /dev/null
  az ad app federated-credential create --id "$app_id" --parameters \
    "{\"name\":\"$2\",\"issuer\":\"$ISSUER\",\"subject\":\"$3\",\"audiences\":[\"$AUDIENCE\"]}" > /dev/null
  echo "$1 appId: $app_id"
}

new_identity fabricops-cicd-test fabricops-gh-env-test "repo:$REPO:environment:test"
new_identity fabricops-cicd-prod fabricops-gh-env-prod "repo:$REPO:environment:prod"
new_identity fabricops-whatif    fabricops-gh-pull-request "repo:$REPO:pull_request"
```

Record the printed ids:

```bash
export DEV_CICD_APP_ID=c3960e5b-b0fd-4bee-a247-667fbc753018
export TEST_CICD_APP_ID=<printed above>
export PROD_CICD_APP_ID=<printed above>
export WHATIF_APP_ID=<printed above>
```

Check that your organisation did not customise the OIDC subject claim; the subjects above assume the default
(`gh api repos/$REPO/actions/oidc/customization/sub` must say `"use_default": true`).

Federated credentials do not accept wildcards, so each subject is registered exactly. Add one more only while you
are doing a branch dry run (see *Dry run on a branch*).

### 4. Azure role assignments (least privilege)

Principle: the identity of an environment can deploy that environment's resource group, pull nothing, push nothing
except to the registry from `dev`, and assign only the two roles the template assigns.

| Identity | Role | Scope | Why |
|---|---|---|---|
| `fabricops-cicd-dev` | `AcrPush` | the registry | Push images (exists) |
| each `fabricops-cicd-<env>` | `Contributor` | `rg-fabricops-<env>` | Create the template's resources |
| each `fabricops-cicd-<env>` | `Role Based Access Control Administrator`, **constrained** to `AcrPull` and `Cognitive Services OpenAI User` | `rg-fabricops-<env>`, the registry, and the Azure OpenAI account when one is used | The template grants the reader identity `AcrPull` on the registry (`modules/acr-pull.bicep`) and, optionally, `Cognitive Services OpenAI User` on the account (`modules/openai-role.bicep`) |
| `fabricops-whatif` | `Reader` + custom `FabricOps Deployment What-If` | `rg-fabricops-dev` and `rg-fabricops-shared` | Simulate the deployment; cannot change anything |

The constrained RBAC Administrator role is the built-in role plus an ABAC condition. With it the identity can create
and delete role assignments only for the two role definitions below, so it cannot grant itself or anyone Owner,
Contributor or any other role:

```bash
ACR_PULL=7f951dda-4ed3-4680-a7ca-43fe172d538d       # AcrPull
OPENAI_USER=5e0bd9bd-7b93-4f28-af87-19fc36ad61bd    # Cognitive Services OpenAI User
CONDITION="((!(ActionMatches{'Microsoft.Authorization/roleAssignments/write'})) OR (@Request[Microsoft.Authorization/roleAssignments:RoleDefinitionId] ForAnyOfAnyValues:GuidEquals {${ACR_PULL}, ${OPENAI_USER}})) AND ((!(ActionMatches{'Microsoft.Authorization/roleAssignments/delete'})) OR (@Resource[Microsoft.Authorization/roleAssignments:RoleDefinitionId] ForAnyOfAnyValues:GuidEquals {${ACR_PULL}, ${OPENAI_USER}}))"

grant_deployer() {   # grant_deployer <env> <cicd app id>
  local env=$1 sp rg_scope
  sp=$(az ad sp show --id "$2" --query id -o tsv)
  rg_scope="/subscriptions/$SUBSCRIPTION_ID/resourceGroups/rg-fabricops-$env"
  az role assignment create --assignee-object-id "$sp" --assignee-principal-type ServicePrincipal \
    --role Contributor --scope "$rg_scope"
  for scope in "$rg_scope" "$ACR_ID"; do
    az role assignment create --assignee-object-id "$sp" --assignee-principal-type ServicePrincipal \
      --role "Role Based Access Control Administrator" --scope "$scope" \
      --condition "$CONDITION" --condition-version 2.0
  done
}

grant_deployer dev  "$DEV_CICD_APP_ID"
grant_deployer test "$TEST_CICD_APP_ID"
grant_deployer prod "$PROD_CICD_APP_ID"
```

When an Azure OpenAI account is added (ADR-0012), run the same `az role assignment create ... "Role Based Access
Control Administrator" --condition "$CONDITION"` for each environment identity with `--scope <account resource id>`.

The what-if identity needs the `Microsoft.Resources/deployments/whatif/action` permission, which no built-in
read-only role includes, so define a custom role and give it `Reader` as well:

```bash
cat > whatif-role.json <<EOF
{
  "Name": "FabricOps Deployment What-If",
  "IsCustom": true,
  "Description": "Run ARM deployment what-if (a read-only simulation). Cannot create or change resources.",
  "Actions": [
    "Microsoft.Resources/deployments/whatif/action",
    "Microsoft.Resources/deployments/read",
    "Microsoft.Resources/deployments/operations/read"
  ],
  "NotActions": [],
  "AssignableScopes": ["/subscriptions/$SUBSCRIPTION_ID"]
}
EOF
az role definition create --role-definition @whatif-role.json
rm whatif-role.json

WHATIF_SP=$(az ad sp show --id "$WHATIF_APP_ID" --query id -o tsv)
for scope in "/subscriptions/$SUBSCRIPTION_ID/resourceGroups/rg-fabricops-dev" \
             "/subscriptions/$SUBSCRIPTION_ID/resourceGroups/$ACR_RG"; do
  for role in Reader "FabricOps Deployment What-If"; do
    az role assignment create --assignee-object-id "$WHATIF_SP" --assignee-principal-type ServicePrincipal \
      --role "$role" --scope "$scope"
  done
done
```

> **To verify on the first pull request.** The template reads keys with `listKeys()` (storage, Log Analytics), and a
> what-if has to evaluate them. If the what-if job reports `AuthorizationFailed` for `listKeys`, the simulation is
> being blocked by the missing read of keys. Do **not** grant the what-if identity key access; the workflow's summary
> would still list only paths. Treat it as a known limit of the preview, report it in the pull request, and keep the
> what-if advisory (it is not a required check).

### 5. Smoke-test identity

The post-deployment smoke test calls the API with a token for the environment's own CI/CD service principal, so that
principal must hold the `FabricOps.Automation` role on that environment's API registration (assigned *to the
service principal, as an application permission*, not to a person):

```bash
assign_automation() {   # assign_automation <API app id> <CI/CD app id>
  local api_sp role cicd_sp
  api_sp=$(az ad sp show --id "$1" --query id -o tsv)
  role=$(az ad app show --id "$1" --query "appRoles[?value=='FabricOps.Automation'].id | [0]" -o tsv)
  cicd_sp=$(az ad sp show --id "$2" --query id -o tsv)
  az rest --method POST \
    --url "https://graph.microsoft.com/v1.0/servicePrincipals/$api_sp/appRoleAssignedTo" \
    --body "{\"principalId\":\"$cicd_sp\",\"resourceId\":\"$api_sp\",\"appRoleId\":\"$role\"}"
}

assign_automation "$DEV_API_APP_ID"  "$DEV_CICD_APP_ID"
assign_automation "$TEST_API_APP_ID" "$TEST_CICD_APP_ID"
assign_automation "$PROD_API_APP_ID" "$PROD_CICD_APP_ID"
```

This needs an Entra role that can assign app roles (Application Administrator, Cloud Application Administrator, or
Global Administrator). The registration has `appRoleAssignmentRequired = true`, so a principal without the role gets
`AADSTS501051` when it asks for a token: that error, in the smoke-test step, means this step was skipped.
The `Automation` role may call read endpoints and `POST /telemetry/collect`; it can never approve or execute.

### 6. GitHub environments

Three environments, each limited to deployments from `main`; `test` and `prod` also need an approver.

> **Plan check (do this first).** Required reviewers and deployment-branch rules on environments of a **private**
> repository need GitHub Pro, Team or Enterprise. On the Free plan the API accepts the call and silently ignores the
> rules. Verify after creating each environment (commands below). Do not merge the CD workflow to `main` until `prod`
> shows a required reviewer, or a push to `main` can deploy to production unreviewed.

```bash
OWNER_ID=$(gh api user --jq .id)     # run as the person who will approve (add more reviewers with more entries)

for ENV in dev test prod; do
  case $ENV in
    dev)  REVIEWERS='[]' ;;
    *)    REVIEWERS="[{\"type\":\"User\",\"id\":$OWNER_ID}]" ;;
  esac
  gh api -X PUT "repos/$REPO/environments/$ENV" --input - <<EOF
{ "reviewers": $REVIEWERS,
  "prevent_self_review": false,
  "deployment_branch_policy": { "protected_branches": false, "custom_branch_policies": true } }
EOF
  gh api -X POST "repos/$REPO/environments/$ENV/deployment-branch-policies" -f name=main -f type=branch
done

# release.yml publishes from a tag, in the dev environment
gh api -X POST "repos/$REPO/environments/dev/deployment-branch-policies" -f name='v*.*.*' -f type=tag
```

`prevent_self_review` is `false` because the repository has one owner: with `true` the person who pushed to `main`
could never approve their own promotion. With two or more maintainers set it to `true`. A "wait timer" is not needed.

Verify (the `protection_rules` output must list `required_reviewers` for `test` and `prod`, and each environment must
list its branch policy):

```bash
for ENV in dev test prod; do
  echo "== $ENV"
  gh api "repos/$REPO/environments/$ENV" --jq '[.protection_rules[] | .type] , .deployment_branch_policy'
  gh api "repos/$REPO/environments/$ENV/deployment-branch-policies" --jq '[.branch_policies[] | .name]'
done
```

### 7. GitHub variables

All variables hold identifiers only. **Do not create any GitHub secret**: no workflow reads one (a test fails if one
does), and no variable may contain a password, key, token or connection string.

Repository variables:

```bash
gh variable set AZURE_TENANT_ID       --repo $REPO --body "$TENANT_ID"
gh variable set AZURE_SUBSCRIPTION_ID --repo $REPO --body "$SUBSCRIPTION_ID"
gh variable set ACR_NAME              --repo $REPO --body "$ACR_NAME"
gh variable set ACR_LOGIN_SERVER      --repo $REPO --body "$ACR_NAME.azurecr.io"
gh variable set ACR_RESOURCE_ID       --repo $REPO --body "$ACR_ID"
gh variable set ACR_PUSH_CLIENT_ID    --repo $REPO --body "$DEV_CICD_APP_ID"      # build job: AcrPush only
gh variable set WHATIF_CLIENT_ID             --repo $REPO --body "$WHATIF_APP_ID"
gh variable set WHATIF_RESOURCE_GROUP        --repo $REPO --body rg-fabricops-dev
gh variable set WHATIF_ENTRA_API_CLIENT_ID   --repo $REPO --body "$DEV_API_APP_ID"
```

Environment variables:

```bash
set_env_vars() {   # set_env_vars <env> <cicd app id> <API app id>
  gh variable set AZURE_CLIENT_ID      --repo $REPO --env "$1" --body "$2"
  gh variable set AZURE_RESOURCE_GROUP --repo $REPO --env "$1" --body "rg-fabricops-$1"
  gh variable set ENTRA_API_CLIENT_ID  --repo $REPO --env "$1" --body "$3"
}
set_env_vars dev  "$DEV_CICD_APP_ID"  "$DEV_API_APP_ID"
set_env_vars test "$TEST_CICD_APP_ID" "$TEST_API_APP_ID"
set_env_vars prod "$PROD_CICD_APP_ID" "$PROD_API_APP_ID"
```

| Variable | Level | Value | Used by |
|---|---|---|---|
| `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID` | repository | tenant and subscription ids | every `azure/login` |
| `ACR_NAME`, `ACR_LOGIN_SERVER`, `ACR_RESOURCE_ID` | repository | the shared registry | build, deploy (`ACR_LOGIN_SERVER`, `ACR_RESOURCE_ID` reach the `.bicepparam` files), release |
| `ACR_PUSH_CLIENT_ID` | repository | client id of `fabricops-cicd-dev` | `cd.yml` build job |
| `WHATIF_CLIENT_ID`, `WHATIF_RESOURCE_GROUP`, `WHATIF_ENTRA_API_CLIENT_ID` | repository | what-if identity, `rg-fabricops-dev`, dev API app id | `whatif.yml` (skipped while `WHATIF_CLIENT_ID` is unset) |
| `AZURE_CLIENT_ID` | environment | that environment's `fabricops-cicd-<env>` client id | deploy and release |
| `AZURE_RESOURCE_GROUP` | environment | `rg-fabricops-<env>` | deploy |
| `ENTRA_API_CLIENT_ID` | environment | that environment's API app registration client id | `.bicepparam`, smoke test token audience |
| `FABRIC_MODE` | environment, optional | `mock` or `rest`; overrides the default in the `.bicepparam` | `prod` first deployment (below) |
| `SMOKE_HEALTH_AUTHENTICATED` | repository or environment, optional | `true` when Container Apps authentication also covers `/health` | smoke test |

Per-environment settings that are not variables (image reference, tags, CPU, retention, `enableWriteActions`) live in
`infra/bicep/env/<env>.bicepparam` and change through a reviewed pull request. `prod` keeps `enableWriteActions =
false`; CI fails if it changes.

### 8. First deployment of each environment

* `dev` and `test` use `fabricMode = 'mock'` by default and need nothing else.
* `prod` defaults to `rest`, which needs Fabric access and the contracts file before the app can start. For the very
  first deployment set `FABRIC_MODE=mock` on the `prod` environment (`gh variable set FABRIC_MODE --repo $REPO --env
  prod --body mock`), let the pipeline deploy, complete `infra/bicep/README.md` sections 4 and 5, then delete the
  variable (`gh variable delete FABRIC_MODE --repo $REPO --env prod`) and promote again.
* The first revision pulls with a fresh managed identity whose `AcrPull` assignment can take a few minutes to
  propagate. The template creates the assignment before the app, but if the first revision shows an image-pull error,
  restart the revision (`az containerapp revision restart`) or re-run the deploy job.

## Day-to-day

### What a merge does

1. `ci.yml` and `cd.yml` start. CD builds and scans; if the scan gate fails, nothing is tagged or deployed.
2. `dev` deploys and is smoke-tested automatically.
3. The `test` job waits. The reviewer is notified by GitHub; **approve it in the run's *Review deployments* dialog**
   (or reject it). Check that the run's commit is the one you mean to promote: a run for an older commit can still be
   waiting when a newer one is merged, and approving the old one would deploy the older digest. Reject superseded runs.
4. After `test` passes its smoke test, the `prod` job waits for approval in the same way.

### Dry run on a branch

A dry run builds, scans and pushes an image, and skips every deployment. The build job has no environment, so its OIDC
subject is the branch, and the branch needs a federated credential first. Before `cd.yml` is on `main`, GitHub will not
run it with `workflow_dispatch` ("only triggers a workflow run if the workflow file exists on the default branch"),
so the workflow also runs on a push to a branch named `cd-dryrun/**`. Such a run never deploys, and it fails at the
login step, harmlessly, while no credential exists for the branch.

```bash
BRANCH=cd-dryrun/ws4            # any name under cd-dryrun/
az ad app federated-credential create --id "$DEV_CICD_APP_ID" --parameters \
  "{\"name\":\"tmp-dry-run\",\"issuer\":\"$ISSUER\",\"subject\":\"repo:$REPO:ref:refs/heads/$BRANCH\",\"audiences\":[\"$AUDIENCE\"]}"
git push origin "HEAD:refs/heads/$BRANCH"          # from a checkout of the commit to test
gh run watch --repo $REPO "$(gh run list --repo $REPO --workflow cd.yml --branch "$BRANCH" --limit 1 --json databaseId --jq '.[0].databaseId')"
# afterwards: remove the credential and the branch
az ad app federated-credential delete --id "$DEV_CICD_APP_ID" --federated-credential-id tmp-dry-run
git push origin --delete "$BRANCH"
```

Once `cd.yml` is on `main`, a manual run also works on any branch: register the subject
`repo:$REPO:ref:refs/heads/<branch>` the same way, then
`gh workflow run cd.yml --repo $REPO --ref <branch> -f deploy=false`.

The dry run pushes the commit's image to the shared registry, tagged with its SHA once it passes the scan gate. Delete
old dry-run images with `az acr repository delete` (or an `acr purge` task) when they are no longer needed.

### Releasing

1. Open a pull request that bumps `version` in `pyproject.toml` and moves the *Unreleased* entries of
   [`CHANGELOG.md`](../CHANGELOG.md) under `## [X.Y.Z] - YYYY-MM-DD`. Merge it and wait for CD on that commit.
2. Tag that commit: `git tag -a vX.Y.Z -m "FabricOps X.Y.Z" <commit> && git push origin vX.Y.Z`.
3. `release.yml` refuses a tag that is not `vMAJOR.MINOR.PATCH`, is not on `main`, differs from the package version,
   has no changelog section, or has no CD run for the commit. It adds the `X.Y.Z` tag to the same digest and creates
   the release with the SBOM, scan report and digest attached. Evidence artifacts are kept for 90 days; tag within
   that window.

### Rolling back

Redeploy the previous digest by hand with the same parameter file (find it in the earlier run's *Image* summary, in
the `image-digest` artifact, or with `az acr repository show-tags`):

```bash
export CONTAINER_IMAGE=crfabricopsshared.azurecr.io/orqentis-fabricops@sha256:<previous digest>
export ACR_LOGIN_SERVER=crfabricopsshared.azurecr.io ACR_RESOURCE_ID="$ACR_ID"
export ENTRA_API_CLIENT_ID=<that environment's API app id>
az deployment group create -g rg-fabricops-prod -p infra/bicep/env/prod.bicepparam
```

(The app keeps one replica and SQLite state, so back up or accept the in-place revision change: see
`infra/bicep/README.md`, *MVP storage and its limits*.) An in-pipeline rollback input is a follow-up.

### Accepting a scan finding

First try to fix it: Dependabot proposes a new base-image digest weekly; a dependency finding is usually a version bump.
If it cannot be fixed and the risk is acceptable, add a reviewed entry and let the pull request carry the reasoning.

* **Image (Trivy):** an entry in [`.trivyignore.yaml`](../.trivyignore.yaml) with the CVE id, a `statement` that names
  the owner and why the path is unreachable or what removes the exception, and `expired_at` at most 90 days ahead.
  Trivy stops applying an entry after that date, so the finding fails the build again.
* **Dependency (pip-audit):** an `[[ignore]]` entry in [`.pip-audit-allowlist.toml`](../.pip-audit-allowlist.toml) with
  `id`, `package`, `justification`, `owner` and `expires` (all required; an expired or incomplete entry is an error).

The gate fails on HIGH and CRITICAL image findings **that have a fix**; findings with no fix yet are listed in the
job summary and the `trivy-report` artifact but do not block (ADR-0016). To fail on those too, set `ignore-unfixed:
false` in the two scan steps.

### Keeping pins current

Dependabot (weekly) proposes new action SHAs (the `# vX.Y.Z` comment updates with them) and base-image digests.
`actionlint` is pinned by version and checksum in `ci.yml`; to bump it, change `ACTIONLINT_VERSION` and
`ACTIONLINT_SHA256` together (the checksum is in `actionlint_<version>_checksums.txt` on the release page).

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `AADSTS70021: No matching federated identity record found` | The run's subject is not registered. The error shows the subject; compare it with the table at the top and create that federated credential on the right app |
| `AuthorizationFailed ... roleAssignments/write` during deployment | The deploying identity lacks the constrained RBAC Administrator on the registry (or OpenAI account) scope, or the template tried to assign a role outside the condition |
| Job fails with `Missing GitHub variables` | Section 7. The message lists the names |
| `Resource group ... does not exist` | Section 1 |
| Deployment fine, smoke test `AADSTS501051` or `403` on `/incidents` | The CI/CD principal lacks the `FabricOps.Automation` role (section 5), or the role is missing on the API registration (section 2) |
| Smoke test `GET /health -> 401` | Container Apps authentication also covers `/health`. Set `SMOKE_HEALTH_AUTHENTICATED=true`, or exclude `/health` from authentication (WS3) |
| First revision `ImagePullBackOff` | `AcrPull` propagation delay: restart the revision |
| Workflow does not start at all | Check the repository's Actions billing and policy settings; jobs cannot start while the account has a billing problem |
