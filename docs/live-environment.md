# Live environment

Record of every change the programme makes in the live Fabric tenant and Azure subscription:
what, when, why, and how to undo it. No secrets, tokens or tenant data are recorded here.

Rules (from the programme brief):

* Everything created is prefixed `fabricops-` / `FabricOps Sandbox` and tagged
  `owner=fabricops-programme`, `purpose=sandbox|dev|test|prod` (`purpose=shared` for resources used
  by every environment, such as the container registry). Entra applications carry the same
  information in their `notes` field. Fabric capacity names allow only
  lowercase letters and digits, so capacities use `fabricops` without the hyphen.
* Existing workspaces and resources not created by the programme are never modified, read for
  data, or granted access. Reading the metadata of a workspace approved for monitoring is allowed.
* The runtime never uses the owner's identity. The owner's Global Administrator sign-in is used for
  bootstrap only, after explicit approval of each change.
* The teardown script (`scripts/live-estate/teardown.sh`, added in WS5) is kept idempotent and
  current with this log.

## Discovery snapshot (read-only, 2026-10-20)

| Item | Finding |
|---|---|
| Tenant | Datachain Consulting, `4b7e9d9f-8e54-43da-a1dd-2d1cc793721a`. Same tenant as every Azure subscription |
| Default subscription | *Sandbox Environment 2026* (`c8ce2a82-b8b8-4b82-b493-1f82ec33b961`). The tenant also hosts other production subscriptions |
| Region | Australia East. Every required resource provider is registered (App, DBforPostgreSQL, ContainerRegistry, Fabric, OperationalInsights, KeyVault, Storage, CognitiveServices) |
| Fabric capacities | Two paused F SKUs that belong to other projects, one active Fabric trial capacity (Southeast Asia) and one PPU. None created by the programme |
| Workspaces | 7 shared workspaces plus personal workspaces. None belong to the programme. The Capacity Metrics app is installed on a paused capacity |
| Tenant settings | *Service principals can call Fabric public APIs*: on (whole organisation). *SP read-only admin APIs*: on, one security group only. *SP can create workspaces, connections, deployment pipelines*: off. *Workspace monitoring*: on. *Semantic model Execute Queries REST API*: on. *OneLake access from apps external to Fabric*: on |
| Azure OpenAI | No programme resource. Australia East offers `gpt-5.5` and `gpt-5.6-*` (GlobalStandard) and `gpt-5.4` / `gpt-5.6-sol` (DataZoneStandard), with quota available |
| Teams | Teams licensing for one user (Teams Exploratory); Power Automate (free) available for Workflows webhooks |
| Budgets | None on the sandbox subscription |

## Change log

| Date (UTC) | Change | Why | Approved | Undo |
|---|---|---|---|---|
| 2026-10-20 | GitHub repository default branch set to `main` | Programme PRs, Dependabot and defaults target `main` (B2) | Owner, 2026-10-20 | `gh repo edit mjtpena/fabricops-digital-agent --default-branch claude/amazing-feynman-sbw9fs` |
| 2026-10-20 | Azure budget `fabricops-programme` on subscription *Sandbox Environment 2026*: AUD 1,100/month over `rg-fabricops-*` and `RG-FABRICDEMO`; alerts at 50/80/100% actual and 100% forecast to the owner's work email | Cost cap D5 (USD 750; billing currency is AUD) | Owner (A5, D5) | `az rest --method delete --url "https://management.azure.com/subscriptions/c8ce2a82-b8b8-4b82-b493-1f82ec33b961/providers/Microsoft.Consumption/budgets/fabricops-programme?api-version=2023-05-01"` |
| 2026-10-20 | Resource group `rg-fabricops-shared` (Australia East, tags `owner=fabricops-programme`, `purpose=shared`) | Shared registry for all environments (ADR-0011) | Owner (A6) | `az group delete -n rg-fabricops-shared --subscription c8ce2a82-b8b8-4b82-b493-1f82ec33b961` |
| 2026-10-20 | Container registry `crfabricopsshared` (Basic, admin user disabled) in `rg-fabricops-shared` | Images built once, promoted by digest (WS4) | Owner (A6) | Deleted with the resource group, or `az acr delete -n crfabricopsshared` |
| 2026-10-20 | Entra app + service principal `fabricops-cicd-dev` (appId `c3960e5b-b0fd-4bee-a247-667fbc753018`), no secrets; federated credentials `gh-env-dev` (`repo:mjtpena/fabricops-digital-agent:environment:dev`) and `gh-ref-main` (`...:ref:refs/heads/main`) | GitHub Actions OIDC identity for `dev` (WS4) | Owner (A6) | `az ad app delete --id c3960e5b-b0fd-4bee-a247-667fbc753018` |
| 2026-10-20 | Role assignment `AcrPush` for `fabricops-cicd-dev` on `crfabricopsshared` (assignment `a7da63e6-0625-46a7-9117-01a63738221d`) | Push images from CD | Owner (A6) | `MSYS_NO_PATHCONV=1 az role assignment delete --ids /subscriptions/c8ce2a82-b8b8-4b82-b493-1f82ec33b961/resourceGroups/rg-fabricops-shared/providers/Microsoft.ContainerRegistry/registries/crfabricopsshared/providers/Microsoft.Authorization/roleAssignments/a7da63e6-0625-46a7-9117-01a63738221d` |
| 2026-10-20 | Entra app + service principal `fabricops-live-reader` (appId `c4764336-c6b6-45fc-9cd8-d83bcff1e353`); one client secret, expires 2026-11-19, stored only on the orchestrator machine outside the repository | Least-privilege identity for live tests and permission discovery (WS5); Fabric grants are logged as they are made | Owner (A4) | `az ad app delete --id c4764336-c6b6-45fc-9cd8-d83bcff1e353` |
