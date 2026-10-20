// Parameters for the PROD environment (resource group rg-fabricops-prod). Used by .github/workflows/cd.yml (after the
// `prod` GitHub environment's required reviewers approve) and bicep-diff.yml; see docs/delivery.md.
// Contains no secrets and must never contain any.
//
//   Values supplied by the pipeline (GitHub environment/repository variables, all identifiers, none secret):
//     CONTAINER_IMAGE      <registry>/orqentis-fabricops@sha256:<digest>   the image already running in test
//     ACR_LOGIN_SERVER     login server of the shared registry
//     ACR_RESOURCE_ID      full resource id of the shared registry (the template grants the reader identity AcrPull)
//     ENTRA_API_CLIENT_ID  client id of the "Orqentis FabricOps API" app registration for prod
//     FABRIC_MODE          optional override of fabricMode ('mock' or 'rest')
//     POSTGRES_ENTRA_ADMIN_OBJECT_ID  object id of the Entra group (or user or service principal) that administers this
//                          environment's PostgreSQL server (an identifier, not a secret)
//     POSTGRES_ENTRA_ADMIN_NAME       its name; for a group the display name, which is the PostgreSQL role name people use
//     POSTGRES_ENTRA_ADMIN_TYPE       optional: Group (default), User or ServicePrincipal
//     POSTGRES_EXISTING_SERVER_NAME   optional: use this existing server (for example a restore) instead of the one the
//                          template creates (docs/operations/restore.md)
//   There are no defaults for CONTAINER_IMAGE, ACR_LOGIN_SERVER, ACR_RESOURCE_ID, ENTRA_API_CLIENT_ID,
//   POSTGRES_ENTRA_ADMIN_OBJECT_ID and POSTGRES_ENTRA_ADMIN_NAME: a missing variable fails the deployment instead of
//   deploying a wrong value. `bash scripts/validate-bicep.sh` supplies placeholders so the file can be compiled locally.

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

// PostgreSQL (Microsoft Entra authentication only, no password). The Entra administrator is required: the group whose
// members run the one-time bootstrap step that creates the app's database role (infra/bicep/README.md, "PostgreSQL
// bootstrap"). Identifiers only; they come from the environment's GitHub variables.
param postgresEntraAdminObjectId = readEnvironmentVariable('POSTGRES_ENTRA_ADMIN_OBJECT_ID')
param postgresEntraAdminName = readEnvironmentVariable('POSTGRES_ENTRA_ADMIN_NAME')
param postgresEntraAdminType = readEnvironmentVariable('POSTGRES_ENTRA_ADMIN_TYPE', 'Group')
// Normally empty. After a point-in-time restore, the name of the restored server: the app is then pointed at it and the
// template changes nothing about it (docs/operations/restore.md).
param postgresExistingServerName = readEnvironmentVariable('POSTGRES_EXISTING_SERVER_NAME', '')

param deploymentEnvironment = 'prod'
// 'rest' reads the real Fabric estate and needs the contracts file (a blob, uploaded after the first deployment)
// before the app can become ready. For the first deployment, before Fabric access and the contracts file are in place,
// set the FABRIC_MODE variable on the `prod` GitHub environment to 'mock', then delete the variable once the grants and
// the file exist (see infra/bicep/README.md, sections 4 and 5).
param fabricMode = readEnvironmentVariable('FABRIC_MODE', 'rest')
// The estate contracts file is a blob in the evidence storage account (container 'contracts', blob 'estate.json' by
// default; infra/bicep/README.md, Estate contracts). In rest mode the app stays not ready until a valid file has been
// uploaded there; mock mode does not read it.

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

param containerCpu = '0.5'
param containerMemory = '1Gi'
param logRetentionInDays = 90

// Scale: two replicas. FabricOps is low-traffic and its scheduled work runs on the lease holder only, so a third
// replica adds no capacity; it would only multiply the per-replica API rate limits (kept in memory) and the database
// connections. Raise maxReplicas only for a measured reason.
param minReplicas = 2
param maxReplicas = 2

// Evidence storage account: Blob, Entra only, zone-redundant, version-level immutability. The policy stays Unlocked
// until the owner approves locking it: Locked is irreversible (retention can then only be extended, and the account
// cannot be deleted until every retention has expired). To lock: set 'Locked' here in a reviewed pull request.
// 365 days is the proposed retention for the owner to confirm before locking.
param storageSkuName = 'Standard_ZRS'
param evidenceRetentionDays = 365
param evidenceImmutabilityState = 'Unlocked'

// PostgreSQL: General Purpose compute, the longest point-in-time restore window. High availability and geo-redundant
// backup are off until the owner decides: ZoneRedundant HA doubles the compute cost, and geo-redundant backup copies
// backups to the paired region (a data-residency decision, fixed when the server is created).
param postgresSkuName = 'Standard_D2ds_v5'
param postgresStorageSizeGiB = 64
param postgresBackupRetentionDays = 35
param postgresHighAvailabilityMode = 'Disabled'
param postgresGeoRedundantBackup = false
// Public access with no firewall rule would leave the app unable to connect: Container Apps has no fixed outbound
// address. This adds the 0.0.0.0-0.0.0.0 rule, which only opens the TCP port to Azure services; every connection still
// needs a valid Entra token for a database role. A private prod (postgresDelegatedSubnetId + postgresPrivateDnsZoneId,
// with ingressMode 'vnet') is the owner's decision, like the ingress mode above.
param postgresAllowAzureServices = true
