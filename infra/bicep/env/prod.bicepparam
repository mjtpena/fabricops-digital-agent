// Parameters for the PROD environment (resource group rg-fabricops-prod). Used by .github/workflows/cd.yml (after the
// `prod` GitHub environment's required reviewers approve) and whatif.yml; see docs/delivery.md.
// Contains no secrets and must never contain any.
//
//   Values supplied by the pipeline (GitHub environment/repository variables, all identifiers, none secret):
//     CONTAINER_IMAGE      <registry>/orqentis-fabricops@sha256:<digest>   the image already running in test
//     ACR_LOGIN_SERVER     login server of the shared registry
//     ACR_RESOURCE_ID      full resource id of the shared registry (the template grants the reader identity AcrPull)
//     ENTRA_API_CLIENT_ID  client id of the "Orqentis FabricOps API" app registration for prod
//     FABRIC_MODE          optional override of fabricMode ('mock' or 'rest')
//   There are no defaults for the first four: a missing variable fails the deployment instead of deploying a wrong
//   value. `bash scripts/validate-bicep.sh` supplies placeholders so the file can be compiled locally.

using '../main.bicep'

param prefix = 'fabricops-prod'

param tags = {
  application: 'orqentis-fabricops'
  environment: 'prod'
  purpose: 'prod'
  owner: 'fabricops-programme'
}

param containerImage = readEnvironmentVariable('CONTAINER_IMAGE')
param containerRegistryServer = readEnvironmentVariable('ACR_LOGIN_SERVER')
param containerRegistryResourceId = readEnvironmentVariable('ACR_RESOURCE_ID')

param entraClientId = readEnvironmentVariable('ENTRA_API_CLIENT_ID')
// param entraTenantId = '<tenant id>'   // defaults to the deployment tenant

param deploymentEnvironment = 'prod'
// 'rest' reads the real Fabric estate and needs the contracts file at contractsPath before the app can start. For
// the first deployment, before Fabric access and the contracts file are in place, set the FABRIC_MODE variable on the
// `prod` GitHub environment to 'mock', then delete the variable once the grants and the file exist (see
// infra/bicep/README.md, sections 4 and 5).
param fabricMode = readEnvironmentVariable('FABRIC_MODE', 'rest')
param contractsPath = '/var/lib/orqentis/contracts/estate.json'

// PRODUCTION WRITES STAY DISABLED. The write (actor) identity is created but never attached to the app.
// scripts/validate-bicep.sh, run in CI, fails if this value changes. Enabling writes in prod is an architecture
// decision: it needs the executor (WS11), an ADR and the owner's approval.
param enableWriteActions = false

// Deterministic reasoner. To use the Agent Framework reasoner set all three Azure OpenAI values.
param reasoner = 'rule_based'
param azureOpenAiEndpoint = ''
param azureOpenAiDeployment = ''
param azureOpenAiResourceId = ''

// API exposure (WS3): a public endpoint where every request needs an Entra token (unauthenticated callers get 401).
// Whether prod should be public is the owner's decision: for a private prod set ingressMode = 'vnet' with
// infrastructureSubnetId and run the smoke test from a runner inside the network. The GitHub-hosted runner must reach
// the endpoint for the post-deployment smoke test, so no allowedIpRanges are set (they would deny the runner), and
// /health and /health/ready are excluded from authentication, which is what the smoke test assumes.
// If allowedClientApplications is ever set, it must include this environment's fabricops-deploy-* client id.
param ingressMode = 'externalAuthenticated'
param anonymousHealthEndpoints = true
param infrastructureSubnetId = ''

// Autonomous runtime (WS1): the app collects, detects, investigates, sweeps SLAs and refreshes its catalog on its own.
// It stays on outside local development. A deploy runs the old and the new revision side by side on the same Azure
// Files volume until the PostgreSQL system of record exists (infra/bicep/README.md, "Deploying an update"): the start-up
// delay lets the old revision drain first, and the audit chain can be verified afterwards with GET /audit/verify.
param schedulerEnabled = true
param schedulerStartupDelaySeconds = 120

// IANA time zone for time-of-day reasoning and report times.
param defaultTimezone = 'Australia/Sydney'

// Notifications and the web UI (WS8, ADR-0018). Nothing is sent unless a notifier is configured. Webhook URLs and the sign-in
// client secret are Key Vault references (secret URIs, no version), never values: docs/teams-notifications.md.
param notifierKind = 'null'
param uiEnabled = true
// param notifierKind = 'teams_workflows'
// param notifyTeamsWebhookSecretUri = 'https://<vault>.vault.azure.net/secrets/teams-webhook-url'
// param keyVaultResourceId = '/subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.KeyVault/vaults/<vault>'
// param publicBaseUrl = 'https://fabricops.example.com'   // default: derived from the Container Apps environment
// param uiSignInClientSecretUri = 'https://<vault>.vault.azure.net/secrets/fabricops-ui-client-secret'   // browser sign-in

param containerCpu = '0.5'
param containerMemory = '1Gi'
param logRetentionInDays = 90
param storageSkuName = 'Standard_ZRS'
param fileShareQuotaGiB = 32
