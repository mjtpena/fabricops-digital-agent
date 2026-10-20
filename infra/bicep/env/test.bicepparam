// Parameters for the TEST environment (resource group rg-fabricops-test). Used by .github/workflows/cd.yml and
// whatif.yml; see docs/delivery.md. Contains no secrets and must never contain any.
//
//   Values supplied by the pipeline (GitHub environment/repository variables, all identifiers, none secret):
//     CONTAINER_IMAGE      <registry>/orqentis-fabricops@sha256:<digest>   the image promoted by digest
//     ACR_LOGIN_SERVER     login server of the shared registry
//     ACR_RESOURCE_ID      full resource id of the shared registry (the template grants the reader identity AcrPull)
//     ENTRA_API_CLIENT_ID  client id of the "Orqentis FabricOps API" app registration for this environment
//     FABRIC_MODE          optional override of fabricMode ('mock' or 'rest')
//   There are no defaults for the first four: a missing variable fails the deployment instead of deploying a wrong
//   value. `bash scripts/validate-bicep.sh` supplies placeholders so the file can be compiled locally.

using '../main.bicep'

param prefix = 'fabricops-test'

param tags = {
  application: 'orqentis-fabricops'
  environment: 'test'
  purpose: 'test'
  owner: 'fabricops-programme'
}

param containerImage = readEnvironmentVariable('CONTAINER_IMAGE')
param containerRegistryServer = readEnvironmentVariable('ACR_LOGIN_SERVER')
param containerRegistryResourceId = readEnvironmentVariable('ACR_RESOURCE_ID')

param entraClientId = readEnvironmentVariable('ENTRA_API_CLIENT_ID')
// param entraTenantId = '<tenant id>'   // defaults to the deployment tenant

param deploymentEnvironment = 'test'
// 'mock' serves the synthetic estate baked into the image, so test needs no Fabric access or contracts file.
param fabricMode = readEnvironmentVariable('FABRIC_MODE', 'mock')
param contractsPath = '/var/lib/orqentis/contracts/estate.json'

// The WRITE capability boundary stays closed in every environment until the executor ships (WS11).
param enableWriteActions = false

// Deterministic reasoner. To use the Agent Framework reasoner set all three Azure OpenAI values.
param reasoner = 'rule_based'
param azureOpenAiEndpoint = ''
param azureOpenAiDeployment = ''
param azureOpenAiResourceId = ''

// API exposure (WS3): a public endpoint where every request needs an Entra token (unauthenticated callers get 401). The
// GitHub-hosted runner must reach it for the post-deployment smoke test, so no allowedIpRanges are set (they would deny
// the runner), and /health and /health/ready are excluded from authentication, which is what the smoke test assumes.
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

param containerCpu = '0.25'
param containerMemory = '0.5Gi'
param logRetentionInDays = 30
param storageSkuName = 'Standard_LRS'
param fileShareQuotaGiB = 8

// Alerts (WS9, docs/operations.md): an action group, six alert rules and a workbook are deployed with the app.
// Who is told is NOT in this file: parameter files never hold people or webhooks. Set these in the environment of
// whoever deploys (the CD pipeline does not set them yet; docs/operations.md, "Alert receivers"):
//   ALERT_EMAIL_RECEIVERS     comma-separated addresses, for example "oncall@example.com,team@example.com"
//   ALERT_TEAMS_WEBHOOK_URI   a Teams Workflows webhook URL (a secret: keep it in the deployer's secret store)
// Both empty: the rules still evaluate and show in the portal, but nobody is notified.
var alertEmailList = readEnvironmentVariable('ALERT_EMAIL_RECEIVERS', '')
param alertEmailReceivers = empty(alertEmailList) ? [] : split(alertEmailList, ',')
param alertTeamsWebhookUri = readEnvironmentVariable('ALERT_TEAMS_WEBHOOK_URI', '')
