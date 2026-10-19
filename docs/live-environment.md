# Live environment

Record of every change the programme makes in the live Fabric tenant and Azure subscription:
what, when, why, and how to undo it. No secrets, tokens or tenant data are recorded here.

Rules (from the programme brief):

* Everything created is prefixed `fabricops-` / `FabricOps Sandbox` and tagged
  `owner=fabricops-programme`, `purpose=sandbox|dev|test|prod`. Fabric capacity names allow only
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
| | *(no changes yet)* | | | |
