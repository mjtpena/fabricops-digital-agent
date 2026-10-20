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
param contractsPath = '/var/lib/orqentis/contracts/estate.json'

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
// Seconds before the scheduler takes the lease after start, so the previous revision can drain during a deploy.
param schedulerStartupDelaySeconds = 120
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

param logRetentionInDays = 30
param storageSkuName = 'Standard_LRS'
param fileShareQuotaGiB = 32

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

// Monitoring (WS9): action group, six alert rules and a workbook. See infra/bicep/README.md and docs/operations.md.
// Who is told comes from the deployer's environment, never from this file (see infra/bicep/env/dev.bicepparam).
param monitoringEnabled = true
param alertEmailReceivers = []                  // for example ['oncall@example.com']; real addresses stay out of the repository
// param alertTeamsWebhookUri = readEnvironmentVariable('ALERT_TEAMS_WEBHOOK_URI', '')   // a secret: never write the URL here
// Thresholds and windows (the values below are the defaults). Windows are limited to what Azure accepts: 5, 10, 15, 30,
// 45, 60, 120, 180, 240, 300, 360, 1440 or 2880 minutes, and none may be shorter than alertEvaluationFrequencyMinutes.
param alertEvaluationFrequencyMinutes = 5
param alertAppDownWindowMinutes = 5             // no running replica for this long
param alertRestartWindowMinutes = 15            // restart loop: alertRestartThreshold restarts within this window
param alertRestartThreshold = 3
param alertHttp5xxWindowMinutes = 10            // 5xx rate: alertHttp5xxRatePercent of the requests, once there are enough
param alertHttp5xxRatePercent = 5
param alertHttp5xxMinRequests = 20
param alertCollectorErrorWindowMinutes = 15     // collector errors: alertCollectorErrorRuns failing runs of one collector
param alertCollectorErrorRuns = 2
param alertHeartbeatMinutes = 15                // no successful collection for this long
param alertAuditFailureWindowMinutes = 0        // 0: derived from auditVerifyIntervalMinutes (twice the interval, rounded up to a window Azure offers)
param auditVerifyEnabled = true
param auditVerifyIntervalMinutes = 60
// Log Analytics daily cap in GB (-1: none). Caps stop ingestion when reached, alerts included: dev only.
param logDailyQuotaGb = -1
// Estimated model cost (USD per million tokens) for the cost metric; empty: tokens are counted, cost is not.
param llmInputCostPerMillionTokens = ''
param llmOutputCostPerMillionTokens = ''
