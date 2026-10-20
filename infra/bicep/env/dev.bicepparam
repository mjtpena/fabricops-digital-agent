// Parameters for the DEV environment (resource group rg-fabricops-dev). Used by .github/workflows/cd.yml and
// bicep-diff.yml; see docs/delivery.md. Contains no secrets and must never contain any.
//
//   Values supplied by the pipeline (GitHub environment/repository variables, all identifiers, none secret):
//     CONTAINER_IMAGE      <registry>/orqentis-fabricops@sha256:<digest>   the image promoted by digest
//     ACR_LOGIN_SERVER     login server of the shared registry
//     ACR_RESOURCE_ID      full resource id of the shared registry (the template grants the reader identity AcrPull)
//     ENTRA_API_CLIENT_ID  client id of the "Orqentis FabricOps API" app registration for this environment
//     FABRIC_MODE          optional override of fabricMode ('mock' or 'rest')
//     POSTGRES_ENTRA_ADMIN_OBJECT_ID  object id of the Entra group (or user or service principal) that administers this
//                          environment's PostgreSQL server (an identifier, not a secret)
//     POSTGRES_ENTRA_ADMIN_NAME       its name; for a group the display name, which is the PostgreSQL role name people use
//     POSTGRES_ENTRA_ADMIN_TYPE       optional: Group (default), User or ServicePrincipal
//     POSTGRES_EXISTING_SERVER_NAME   optional: use this existing server (for example a restore) instead of the one the
//                          template creates (docs/operations/restore.md)
//     EVIDENCE_IMMUTABILITY_LOCKED    optional, default 'false': 'true' locks the evidence immutability policy (IRREVERSIBLE
//                          for the retention period; infra/bicep/README.md, Storage). Any other value fails the build.
//     AUDIT_OPERATORS_OBJECT_ID       optional: object id of the Entra group of operators who may open an audit epoch after a
//                          restore; it gets Storage Blob Data Contributor on the audit-epochs container only
//   There are no defaults for CONTAINER_IMAGE, ACR_LOGIN_SERVER, ACR_RESOURCE_ID, ENTRA_API_CLIENT_ID,
//   POSTGRES_ENTRA_ADMIN_OBJECT_ID and POSTGRES_ENTRA_ADMIN_NAME: a missing variable fails the deployment instead of
//   deploying a wrong value. `bash scripts/validate-bicep.sh` supplies placeholders so the file can be compiled locally.

using '../main.bicep'

param prefix = 'fabricops-dev'

param tags = {
  application: 'orqentis-fabricops'
  environment: 'dev'
  purpose: 'dev'
  owner: 'fabricops-programme'
}

param containerImage = readEnvironmentVariable('CONTAINER_IMAGE')
param containerRegistryServer = readEnvironmentVariable('ACR_LOGIN_SERVER')
param containerRegistryResourceId = readEnvironmentVariable('ACR_RESOURCE_ID')

param entraClientId = readEnvironmentVariable('ENTRA_API_CLIENT_ID')
// param entraTenantId = '<tenant id>'   // defaults to the deployment tenant

// PostgreSQL (Microsoft Entra authentication only, no password). The Entra administrator is required: the group whose
// members run the one-time bootstrap step that creates the app's database role (infra/bicep/README.md, "PostgreSQL
// bootstrap"). Identifiers only; they come from the environment's GitHub variables.
param postgresEntraAdminObjectId = readEnvironmentVariable('POSTGRES_ENTRA_ADMIN_OBJECT_ID')
param postgresEntraAdminName = readEnvironmentVariable('POSTGRES_ENTRA_ADMIN_NAME')
param postgresEntraAdminType = readEnvironmentVariable('POSTGRES_ENTRA_ADMIN_TYPE', 'Group')
// Normally empty. After a point-in-time restore, the name of the restored server: the app is then pointed at it and the
// template changes nothing about it (docs/operations/restore.md).
param postgresExistingServerName = readEnvironmentVariable('POSTGRES_EXISTING_SERVER_NAME', '')

// Audit epoch markers (README, Storage): the app can only READ the audit-epochs container. Operators who may open an epoch
// after a restore are an Entra group; empty assigns nothing and the owner grants it.
param auditOperatorsPrincipalId = readEnvironmentVariable('AUDIT_OPERATORS_OBJECT_ID', '')

param deploymentEnvironment = 'dev'
// 'mock' serves the synthetic estate baked into the image, so dev needs no Fabric access or contracts file.
param fabricMode = readEnvironmentVariable('FABRIC_MODE', 'mock')
// The estate contracts file is a blob in the evidence storage account (container 'contracts', blob 'estate.json' by
// default; infra/bicep/README.md, Estate contracts). In rest mode the app stays not ready until a valid file has been
// uploaded there; mock mode does not read it.

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
// It stays on outside local development. A deploy runs the old and the new revision side by side, which is safe because
// the PostgreSQL system of record serialises the audit chain, the leader lease and approvals (infra/bicep/README.md,
// "Deploying an update"). The start-up delay is a courtesy only: a starting revision does not compete for the lease
// while it warms up.
param schedulerEnabled = true
param schedulerStartupDelaySeconds = 30

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

param containerCpu = '0.25'
param containerMemory = '0.5Gi'
param logRetentionInDays = 30

// Scale: dev runs one replica to keep its cost low (the app is stateless and safe at two or more; test and prod run 2).
param minReplicas = 1
param maxReplicas = 1

// Evidence storage account: Blob, Entra only, version-level immutability. Short retention and Unlocked, so dev
// evidence can be cleaned up by an account owner.
param storageSkuName = 'Standard_LRS'
param evidenceRetentionDays = 7
// The policy is Unlocked unless the optional variable EVIDENCE_IMMUTABILITY_LOCKED says 'true'. Locking is irreversible
// for the retention period, so it is never done here by accident: this mapping is the only place that sets 'Locked'.
var evidenceLocked = readEnvironmentVariable('EVIDENCE_IMMUTABILITY_LOCKED', 'false')
param evidenceImmutabilityState = evidenceLocked == 'true' ? 'Locked' : (evidenceLocked == 'false' ? 'Unlocked' : 'EVIDENCE_IMMUTABILITY_LOCKED must be true or false')

// PostgreSQL: the smallest Burstable server, 7 days of point-in-time restore.
param postgresSkuName = 'Standard_B1ms'
param postgresStorageSizeGiB = 32
param postgresBackupRetentionDays = 7
// Public access with no firewall rule would leave the app unable to connect: Container Apps has no fixed outbound
// address. This adds the 0.0.0.0-0.0.0.0 rule, which only opens the TCP port to Azure services; every connection still
// needs a valid Entra token for a database role. A VNet-integrated server (postgresDelegatedSubnetId) is the
// stricter alternative once dev has a network. Whoever runs the bootstrap step also needs a rule: postgresAllowedIpRanges.
param postgresAllowAzureServices = true

// Alerts (WS9, docs/operations.md): an action group, six alert rules and a workbook are deployed with the app.
// Who is told is NOT in this file: parameter files never hold people or webhooks. Set these in the environment of
// whoever deploys (the CD pipeline does not set them yet; docs/operations.md, "Alert receivers"):
//   ALERT_EMAIL_RECEIVERS     comma-separated addresses, for example "oncall@example.com,team@example.com"
//   ALERT_TEAMS_WEBHOOK_URI   a Teams Workflows webhook URL (a secret: keep it in the deployer's secret store)
// Both empty: the rules still evaluate and show in the portal, but nobody is notified.
var alertEmailList = readEnvironmentVariable('ALERT_EMAIL_RECEIVERS', '')
// Whitespace around an address is trimmed ("a@example.com, b@example.com") and empty entries are dropped.
param alertEmailReceivers = filter(map(split(alertEmailList, ','), address => trim(address)), address => !empty(address))
param alertTeamsWebhookUri = readEnvironmentVariable('ALERT_TEAMS_WEBHOOK_URI', '')

// A daily cap keeps the sandbox bill bounded. Reaching it stops ingestion (the heartbeat alert then fires) until the
// next day; docs/cost.md.
param logDailyQuotaGb = 1
