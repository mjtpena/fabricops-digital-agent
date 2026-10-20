// Example parameters for infra/bicep/main.bicep. Copy, adjust and deploy:
//
//   az deployment group create -g rg-fabricops-prod \
//     -f infra/bicep/main.bicep -p infra/bicep/main.bicepparam
//
// Contains no secrets and must never contain any. Values below are placeholders.

using './main.bicep'

param prefix = 'fabricops'

param tags = {
  application: 'orqentis-fabricops'
  environment: 'prod'
}

// Image built from the repository Dockerfile and pushed to an ACR in the same resource group.
param containerImage = 'crfabricops.azurecr.io/orqentis-fabricops:0.1.0'
param containerRegistryName = 'crfabricops'

// Microsoft Entra app registration that represents the API (app roles FabricOps.Operator, FabricOps.PlatformAdmin,
// FabricOps.Automation and FabricOps.Reader).
param entraClientId = '00000000-0000-0000-0000-000000000000'
// param entraTenantId = '<tenant id>'   // defaults to the deployment tenant

param deploymentEnvironment = 'prod'
param fabricMode = 'rest'
// The estate contracts file (rest mode) is a blob in the evidence storage account, uploaded after the deployment
// (infra/bicep/README.md, Estate contracts); until it is there a rest-mode app stays not ready. These are the defaults.
param contractsContainerName = 'contracts'
param contractsBlobName = 'estate.json'

// The WRITE capability boundary stays closed: the actor identity is created but not attached.
param enableWriteActions = false

// Deterministic reasoner by default. For the Agent Framework reasoner set all three Azure OpenAI values.
param reasoner = 'rule_based'
param azureOpenAiEndpoint = ''
param azureOpenAiDeployment = ''
param azureOpenAiResourceId = ''
// param reasoner = 'agent_framework'
// param azureOpenAiEndpoint = 'https://aoai-fabricops.openai.azure.com/'
// param azureOpenAiDeployment = 'gpt-4.1'
// param azureOpenAiResourceId = '/subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.CognitiveServices/accounts/aoai-fabricops'

// Autonomous runtime: the app collects, detects, investigates, sweeps SLAs and refreshes its catalog by itself.
// Every value below is the default; set schedulerEnabled = false to run the app API-driven only.
param schedulerEnabled = true
// Seconds before the scheduler takes the lease after start. A courtesy only: the database lease already allows a single
// holder, this just keeps a starting revision from competing for it while it warms up during a deploy.
param schedulerStartupDelaySeconds = 30
param schedulerTickSeconds = 5
param schedulerLeaseTtlSeconds = 60       // at least 3x the tick
param schedulerJobTimeoutMinutes = 30
param collectEnabled = true
param collectIntervalMinutes = 5
param autoInvestigateEnabled = true
param autoInvestigateIntervalMinutes = 1
param autoInvestigateBatchSize = 5
param autoInvestigateMaxAttempts = 3
param slaSweepEnabled = true
param slaSweepIntervalMinutes = 15
param catalogRefreshEnabled = true
param catalogRefreshIntervalMinutes = 60
param investigationMaxConcurrency = 2
param investigationTimeoutSeconds = 600
// Audit chain anchors (immutable blobs) and durable investigation jobs shared by all replicas.
param auditAnchorEnabled = true
param auditAnchorIntervalMinutes = 15
param investigationPollIntervalSeconds = 2
param investigationClaimTtlSeconds = 120   // at least 3x the poll interval
// Global budget for Fabric / Power BI calls (shared by every job) and incremental-collection windows.
param fabricMaxConcurrency = 4
param fabricRatePerSecond = 5
param fabricRateBurst = 10
param fabricJobInstancesMaxPages = 10
param collectionMaxLookbackHours = 168
param collectionOverlapMinutes = 15
// The container is built in the background: wait at most this long for the first attempt, then retry with backoff.
param startupGraceSeconds = 10
param buildRetryInitialSeconds = 2
param buildRetryMaxSeconds = 60

// Exposure: 'externalAuthenticated' (default: public endpoint, every request needs an Entra ID token),
// 'vnet' (internal environment, needs infrastructureSubnetId) or 'internal' (environment-internal only).
// See infra/bicep/README.md "Network exposure" and the examples in infra/bicep/examples/.
param ingressMode = 'externalAuthenticated'
param infrastructureSubnetId = ''
// param allowedIpRanges = ['203.0.113.0/24']   // optional address allow list in front of authentication
param anonymousHealthEndpoints = true           // /health and /health/ready answer without a token

// Notifications and the web UI (WS8, ADR-0018). Nothing is sent unless a notifier is configured. Webhook URLs and the sign-in
// client secret are Key Vault references (secret URIs, no version), never values: docs/teams-notifications.md.
param notifierKind = 'null'
param uiEnabled = true
// param notifierKind = 'teams_workflows'
// param notifyTeamsWebhookSecretUri = 'https://<vault>.vault.azure.net/secrets/teams-webhook-url'
// param keyVaultResourceId = '/subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.KeyVault/vaults/<vault>'
// param publicBaseUrl = 'https://fabricops.example.com'   // default: derived from the Container Apps environment
// param uiSignInClientSecretUri = 'https://<vault>.vault.azure.net/secrets/fabricops-ui-client-secret'   // browser sign-in

param logRetentionInDays = 30

// Scale: the app is stateless (the database serialises the audit chain, the lease and approvals), so two replicas
// are the default. Keep (maxReplicas + minReplicas) x postgresPoolMax + ~15 below the server's max_connections.
param minReplicas = 2
param maxReplicas = 2
param postgresPoolMax = 8

// Evidence storage account (evidence, reports, audit anchors): Blob, Entra only, version-level immutability.
param storageSkuName = 'Standard_LRS'
param evidenceRetentionDays = 30
param evidenceImmutabilityState = 'Unlocked'   // 'Locked' is irreversible: see infra/bicep/README.md, Storage

// PostgreSQL Flexible Server (Microsoft Entra authentication only). The Entra administrator is REQUIRED: a group whose
// members run the bootstrap step (infra/bicep/README.md, "PostgreSQL bootstrap"). Placeholders below.
param postgresEntraAdminObjectId = '00000000-0000-0000-0000-000000000000'
param postgresEntraAdminName = 'sg-fabricops-postgres-admins'
param postgresEntraAdminType = 'Group'
param postgresSkuName = 'Standard_B1ms'
param postgresStorageSizeGiB = 32
param postgresBackupRetentionDays = 7
// Public access: nobody can connect until a firewall rule exists. Azure services = the 0.0.0.0 rule that lets Container
// Apps (no fixed outbound address) reach the port; every connection still needs an Entra token. Alternatively use VNet
// integration: postgresDelegatedSubnetId + postgresPrivateDnsZoneId (and ingressMode 'vnet').
param postgresAllowAzureServices = true
// param postgresAllowedIpRanges = [{ name: 'bootstrap-operator', startIpAddress: '203.0.113.10', endIpAddress: '203.0.113.10' }]

// Time zone for time-of-day reasoning and report times (validated at startup).
param defaultTimezone = 'Australia/Sydney'

// Fabric identity. managed_identity (default) reads Fabric with the reader managed identity. For a Fabric tenant
// that is a different Entra tenant use service_principal with a Key Vault reference: see
// infra/bicep/examples/cross-tenant.bicepparam and "Cross-tenant Fabric access" in infra/bicep/README.md.
param fabricAuthMode = 'managed_identity'

// Sovereign clouds (US Government, China): see "Sovereign clouds" in infra/bicep/README.md. Empty keeps the public cloud.
// param authorityHost = 'https://login.microsoftonline.us'
// param fabricApiBaseUrl = '<Fabric API base URL for your cloud>'
// param powerbiApiBaseUrl = 'https://api.high.powerbigov.us/v1.0/myorg'
// param fabricScope = '<Fabric API scope for your cloud>/.default'
// param powerbiScope = 'https://high.analysis.usgovcloudapi.net/powerbi/api/.default'
