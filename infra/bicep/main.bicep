// Orqentis FabricOps: MVP deployment on Azure Container Apps (one resource group).
//
//   az deployment group create -g <rg> -f infra/bicep/main.bicep -p infra/bicep/main.bicepparam
//
// What this deploys:
//   * Log Analytics workspace + workspace-based Application Insights.
//   * Two user-assigned managed identities, one per capability boundary:
//       id-<prefix>-reader  READ  - the operating identity of the app (Fabric / Power BI read, Azure OpenAI).
//       id-<prefix>-actor   WRITE - only attached to the app when enableWriteActions = true.
//     Fabric workspace roles cannot be granted from ARM/Bicep; see README.md.
//   * Storage account + Azure Files share mounted at /var/lib/orqentis for the MVP local stores (SQLite event
//     store and state DB, evidence files, hash-chained audit log). The target architecture replaces these
//     with Eventhouse (events/audit) and OneLake (evidence).
//   * Container Apps environment (logs to Log Analytics) and the FabricOps Container App, protected by
//     Container Apps built-in authentication (Microsoft Entra ID). The app trusts X-MS-CLIENT-PRINCIPAL,
//     which only that front end can set. Exposure is chosen with ingressMode (default: external and
//     authenticated; see README "Network exposure", including the safe first deployment).
//   * Optional: Cognitive Services OpenAI User for the reader identity on an existing Azure OpenAI account.
//
// No credentials are written anywhere: identities are managed, the Application Insights connection string
// is a Container App secret, and the storage key is read at deployment time for the Azure Files mount only.

targetScope = 'resourceGroup'

// ------------------------------------------------------------------------------------------------- general

@description('Azure region for all resources. Defaults to the resource group location.')
param location string = resourceGroup().location

@description('Short name used in every resource name (id-<prefix>-reader, ca-<prefix>, ...). Lowercase letters, digits and hyphens; start with a letter.')
@minLength(3)
@maxLength(20)
param prefix string = 'fabricops'

@description('Tags applied to every resource.')
param tags object = {
  application: 'orqentis-fabricops'
}

// ------------------------------------------------------------------------------------------------- image

@description('Container image to run, e.g. myregistry.azurecr.io/orqentis-fabricops:0.1.0. Prefer an immutable tag or digest.')
param containerImage string

@description('Optional: name of an Azure Container Registry in THIS resource group. When set, the reader identity is granted AcrPull on it and used to pull the image (no registry passwords).')
param containerRegistryName string = ''

@description('Optional: login server of another private registry (e.g. shared.azurecr.io) that already grants AcrPull to the reader identity. Ignored when containerRegistryName is set. Leave both empty for a public image.')
param containerRegistryServer string = ''

@description('Optional: full resource id of an Azure Container Registry in ANY resource group or subscription, such as the shared registry used by every environment (/subscriptions/<sub>/resourceGroups/rg-fabricops-shared/providers/Microsoft.ContainerRegistry/registries/<name>). When set, the reader identity is granted AcrPull on it before the app is created. Set containerRegistryServer to its login server as well. The deploying principal needs permission to create that role assignment (docs/delivery.md).')
param containerRegistryResourceId string = ''

@description('vCPU for the container (Consumption profile pairs: 0.25/0.5Gi, 0.5/1Gi, 1/2Gi, 2/4Gi).')
@allowed([
  '0.25'
  '0.5'
  '1'
  '2'
])
param containerCpu string = '0.5'

@description('Memory for the container; must match containerCpu (see containerCpu).')
@allowed([
  '0.5Gi'
  '1Gi'
  '2Gi'
  '4Gi'
])
param containerMemory string = '1Gi'

// ------------------------------------------------------------------------------------------------- app settings

@description('ORQENTIS_DEPLOYMENT_ENVIRONMENT. "local" is not offered: outside local the API must use Easy Auth.')
@allowed([
  'dev'
  'test'
  'prod'
])
param deploymentEnvironment string = 'prod'

@description('ORQENTIS_FABRIC_MODE. "rest" reads the real Fabric / Power BI REST APIs with the reader identity; "mock" serves the synthetic demo estate baked into the image.')
@allowed([
  'rest'
  'mock'
])
param fabricMode string = 'rest'

@description('ORQENTIS_CONTRACTS_PATH: declared topology, data products and SLAs (required in rest mode). Default is on the Azure Files share; upload the file before the app starts (see README).')
param contractsPath string = '/var/lib/orqentis/contracts/estate.json'

@description('ORQENTIS_ENABLE_WRITE_ACTIONS. Also attaches the actor (WRITE) identity to the app. Approved actions still go through the policy engine and human approval.')
param enableWriteActions bool = false

@description('ORQENTIS_REASONER. "agent_framework" requires azureOpenAiEndpoint and azureOpenAiDeployment (and normally azureOpenAiResourceId for the role assignment).')
@allowed([
  'rule_based'
  'agent_framework'
])
param reasoner string = 'rule_based'

@description('ORQENTIS_AZURE_OPENAI_ENDPOINT, e.g. https://<account>.openai.azure.com/. Omitted from the app when empty.')
param azureOpenAiEndpoint string = ''

@description('ORQENTIS_AZURE_OPENAI_DEPLOYMENT: the model deployment name. Omitted from the app when empty.')
param azureOpenAiDeployment string = ''

@description('Optional: full resource id of the Azure OpenAI account (/subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.CognitiveServices/accounts/<name>). When set, the reader identity gets "Cognitive Services OpenAI User" on it; it may be in another resource group or subscription.')
param azureOpenAiResourceId string = ''

@description('Price of one million INPUT tokens of the model deployment, in USD (ORQENTIS_LLM_INPUT_COST_PER_MILLION_TOKENS), for the estimated-cost metric. A decimal number as text, taken from the model price list; empty (default) means "not configured": tokens are counted, no cost is emitted.')
param llmInputCostPerMillionTokens string = ''

@description('Price of one million OUTPUT tokens, in USD (ORQENTIS_LLM_OUTPUT_COST_PER_MILLION_TOKENS). See llmInputCostPerMillionTokens.')
param llmOutputCostPerMillionTokens string = ''

// ------------------------------------------------------------------------------------------------- autonomous runtime

@description('ORQENTIS_SCHEDULER_ENABLED. true: the app collects, detects, investigates, sweeps SLAs and refreshes its catalog on a schedule. false: it only acts when called through the API.')
param schedulerEnabled bool = true

@description('Seconds the scheduler waits after the app starts before it takes the leader lease or runs a job (ORQENTIS_SCHEDULER_STARTUP_DELAY_SECONDS). Container Apps runs the old and the new revision side by side during a deploy on the same Azure Files volume; the delay lets the old one drain first. Not a substitute for the database-backed lease: until then deploy with schedulerEnabled = false or accept the risk.')
@minValue(0)
@maxValue(900)
param schedulerStartupDelaySeconds int = 120

@description('Seconds between scheduler ticks, i.e. how often due jobs are started and the lease is renewed (ORQENTIS_SCHEDULER_TICK_SECONDS).')
@minValue(1)
@maxValue(60)
param schedulerTickSeconds int = 5

@description('Leader lease TTL in seconds (ORQENTIS_SCHEDULER_LEASE_TTL_SECONDS). Must be at least three times schedulerTickSeconds, or the app refuses to start.')
@minValue(5)
@maxValue(3600)
param schedulerLeaseTtlSeconds int = 60

@description('Minutes after which a running scheduled job is cancelled (ORQENTIS_SCHEDULER_JOB_TIMEOUT_MINUTES).')
@minValue(1)
@maxValue(1440)
param schedulerJobTimeoutMinutes int = 30

@description('Run the collect + detect job (ORQENTIS_SCHEDULER_COLLECT_ENABLED).')
param collectEnabled bool = true

@description('Minutes between collect + detect runs (ORQENTIS_SCHEDULER_COLLECT_INTERVAL_MINUTES).')
@minValue(1)
@maxValue(1440)
param collectIntervalMinutes int = 5

@description('Investigate new (DETECTED) incidents automatically as background jobs (ORQENTIS_SCHEDULER_AUTO_INVESTIGATE_ENABLED).')
param autoInvestigateEnabled bool = true

@description('Minutes between auto-investigation runs (ORQENTIS_SCHEDULER_AUTO_INVESTIGATE_INTERVAL_MINUTES).')
@minValue(1)
@maxValue(1440)
param autoInvestigateIntervalMinutes int = 1

@description('New investigations queued per auto-investigation run (ORQENTIS_SCHEDULER_AUTO_INVESTIGATE_BATCH_SIZE).')
@minValue(1)
@maxValue(50)
param autoInvestigateBatchSize int = 5

@description('Failed investigation attempts after which an incident is left to a human (ORQENTIS_SCHEDULER_AUTO_INVESTIGATE_MAX_ATTEMPTS).')
@minValue(1)
@maxValue(20)
param autoInvestigateMaxAttempts int = 3

@description('Run the SLA sweep that records SLA state changes as events (ORQENTIS_SCHEDULER_SLA_SWEEP_ENABLED).')
param slaSweepEnabled bool = true

@description('Minutes between SLA sweeps (ORQENTIS_SCHEDULER_SLA_SWEEP_INTERVAL_MINUTES).')
@minValue(1)
@maxValue(1440)
param slaSweepIntervalMinutes int = 15

@description('Reload workspaces, items and dependencies and apply the difference (ORQENTIS_SCHEDULER_CATALOG_REFRESH_ENABLED).')
param catalogRefreshEnabled bool = true

@description('Minutes between catalog refreshes (ORQENTIS_SCHEDULER_CATALOG_REFRESH_INTERVAL_MINUTES).')
@minValue(1)
@maxValue(10080)
param catalogRefreshIntervalMinutes int = 60

@description('Verify the audit hash chain on a schedule; a failure raises the audit-verification alert (ORQENTIS_SCHEDULER_AUDIT_VERIFY_ENABLED).')
param auditVerifyEnabled bool = true

@description('Minutes between audit verifications (ORQENTIS_SCHEDULER_AUDIT_VERIFY_INTERVAL_MINUTES). Each run reads the whole audit log.')
@minValue(1)
@maxValue(10080)
param auditVerifyIntervalMinutes int = 60

@description('Investigations running at once; further ones queue (ORQENTIS_INVESTIGATION_MAX_CONCURRENCY).')
@minValue(1)
@maxValue(16)
param investigationMaxConcurrency int = 2

@description('Seconds after which a running investigation is abandoned and can be retried (ORQENTIS_INVESTIGATION_TIMEOUT_SECONDS).')
@minValue(30)
@maxValue(21600)
param investigationTimeoutSeconds int = 600

@description('Fabric / Power BI calls in flight at once, across all jobs (ORQENTIS_FABRIC_MAX_CONCURRENCY).')
@minValue(1)
@maxValue(64)
param fabricMaxConcurrency int = 4

@description('Steady Fabric / Power BI request rate per second, across all jobs (ORQENTIS_FABRIC_RATE_PER_SECOND). A 429 pauses every caller.')
@minValue(1)
@maxValue(1000)
param fabricRatePerSecond int = 5

@description('Burst allowance on top of the steady request rate (ORQENTIS_FABRIC_RATE_BURST).')
@minValue(1)
@maxValue(1000)
param fabricRateBurst int = 10

@description('Most pages read per job-instance listing; Fabric documents no result order, so listings are filtered, not stopped early (ORQENTIS_FABRIC_JOB_INSTANCES_MAX_PAGES).')
@minValue(1)
@maxValue(1000)
param fabricJobInstancesMaxPages int = 10

@description('Hours of history read the first time a collector runs, and the most a collector ever re-reads after downtime (ORQENTIS_COLLECTION_MAX_LOOKBACK_HOURS).')
@minValue(1)
@maxValue(2160)
param collectionMaxLookbackHours int = 168

@description('Minutes of already-collected history re-read on every run so late-arriving data is not missed; events are de-duplicated (ORQENTIS_COLLECTION_OVERLAP_MINUTES).')
@minValue(0)
@maxValue(1440)
param collectionOverlapMinutes int = 15

@description('Longest the lifespan waits for the first container build attempt before the port opens (ORQENTIS_STARTUP_GRACE_SECONDS). A slow build continues in the background; /health/ready is 503 until it is done.')
@minValue(0)
@maxValue(60)
param startupGraceSeconds int = 10

@description('Seconds before the first retry of a failed container build (ORQENTIS_BUILD_RETRY_INITIAL_SECONDS); the delay doubles up to buildRetryMaxSeconds.')
@minValue(1)
@maxValue(600)
param buildRetryInitialSeconds int = 2

@description('Longest delay between container build retries (ORQENTIS_BUILD_RETRY_MAX_SECONDS); must be at least buildRetryInitialSeconds.')
@minValue(1)
@maxValue(3600)
param buildRetryMaxSeconds int = 60

// ------------------------------------------------------------------------------------------------- network + auth

@description('How the API is exposed. "externalAuthenticated" (default when this and externalIngress are both unset): public endpoint, every request must carry a valid Entra ID token (401 otherwise), optionally limited to allowedIpRanges. "vnet": internal environment with no public IP, reachable from the VNet (requires infrastructureSubnetId). "internal": reachable only from other apps in the Container Apps environment. Authentication is on in every mode. Setting this together with the deprecated externalIngress fails the deployment.')
@allowed([
  'externalAuthenticated'
  'vnet'
  'internal'
])
param ingressMode string?

@description('DEPRECATED, use ingressMode. When set (and ingressMode is not) it keeps the old behaviour so that existing parameter files do not change: false = "internal", true = "externalAuthenticated" (or "vnet" when infrastructureSubnetId is set). Setting both this and ingressMode fails the deployment, so a stale value cannot silently override an explicit mode. Remove it from your parameter files.')
param externalIngress bool?

@description('Optional: resource id of a subnet (delegated to Microsoft.App/environments, /27 or larger) for an internal, VNet-integrated environment with no public endpoint. Required for ingressMode "vnet", must be empty for "externalAuthenticated" (a mismatch fails the deployment), optional for "internal".')
param infrastructureSubnetId string = ''

@description('Optional IPv4 CIDR ranges (e.g. 203.0.113.0/24) allowed to reach the ingress. Empty: no address restriction. When set, every other address is denied before authentication; GitHub-hosted runners and other callers outside the ranges cannot connect.')
param allowedIpRanges array = []

@description('Optional: application (client) ids that may call the API, checked by Easy Auth against the token\'s azp/appid. Empty (default): any client application can call as long as the token is valid for this API. Recommended in production: list the clients people use (e.g. the UI, Azure CLI) and the automation identities\' client ids, so a third-party application cannot act for a user who consents to it.')
param allowedClientApplications array = []

@description('Exclude /health and /health/ready from authentication so uptime checks and smoke tests can call them without a token. They return only the version and mode flags and read no identity. false: every path requires a token.')
param anonymousHealthEndpoints bool = true

@description('ORQENTIS_API_MAX_BODY_BYTES: requests with a larger body get 413.')
@minValue(1024)
@maxValue(16777216)
param apiMaxBodyBytes int = 65536

@description('ORQENTIS_API_RATE_LIMIT_MUTATING_PER_MINUTE: per principal and replica, for approve/reject/execute and other mutating calls (429 + Retry-After beyond it).')
@minValue(1)
@maxValue(10000)
param apiRateLimitMutatingPerMinute int = 30

@description('ORQENTIS_API_RATE_LIMIT_EXPENSIVE_PER_MINUTE: per principal and replica, for telemetry collection, investigations and audit verification.')
@minValue(1)
@maxValue(10000)
param apiRateLimitExpensivePerMinute int = 6

@description('Application (client) id of the Microsoft Entra app registration that represents the FabricOps API (defines the FabricOps.Operator, FabricOps.PlatformAdmin, FabricOps.Automation and FabricOps.Reader app roles).')
param entraClientId string

@description('Microsoft Entra tenant id that issues tokens for the API. Defaults to the deployment tenant.')
param entraTenantId string = tenant().tenantId

@description('ORQENTIS_TENANT_ID: tenant id of the monitored Fabric estate (stamped on every record). Empty (default): the deployment tenant. REQUIRED in service_principal mode: set it to the Fabric tenant, which is a different Entra tenant (see fabricAuthMode); the deployment fails otherwise.')
param fabricTenantId string = ''

@description('ORQENTIS_DEFAULT_TIMEZONE: IANA time zone used for time-of-day reasoning and report times (for example Europe/London). Validated when the app starts; the default keeps the previous behaviour.')
@minLength(1)
param defaultTimezone string = 'Australia/Sydney'

// ------------------------------------------------------------------------------------------------- fabric identity

@description('ORQENTIS_AUTH_MODE for the Fabric and Power BI APIs. managed_identity: the reader managed identity (the Fabric tenant is the Azure tenant). service_principal: an app registration in the Fabric tenant, for a Fabric tenant other than the one hosting this deployment (a managed identity cannot be granted access across tenants). The credential comes from Key Vault, never from the parameters.')
@allowed([
  'managed_identity'
  'service_principal'
])
param fabricAuthMode string = 'managed_identity'

@description('service_principal mode: application (client) id of the service principal in the Fabric tenant (AZURE_CLIENT_ID). Not a secret.')
param fabricServicePrincipalClientId string = ''

@description('service_principal mode, option 1: URI of the Key Vault SECRET holding the client secret, for example https://<vault>.vault.azure.net/secrets/fabric-sp-secret (no version, so rotations are picked up). Resolved by Container Apps with the reader identity. Set this or the certificate URI, not both.')
param fabricServicePrincipalSecretUri string = ''

@description('service_principal mode, option 2: URI of the Key Vault secret that holds the client certificate AND private key as PEM (create the Key Vault certificate with content type application/x-pem-file; its secret is then the PEM). Mounted read-only at /mnt/fabric-sp. Set this or the secret URI, not both.')
param fabricServicePrincipalCertificateUri string = ''

@description('Optional: full resource id of the Key Vault named in the URIs above. When set, the reader identity is granted "Key Vault Secrets User" on the secret(s) named in the URIs above and on nothing else in the vault (the vault must use Azure RBAC; it may be in another resource group or subscription). Leave empty if access is granted some other way.')
param keyVaultResourceId string = ''

// ------------------------------------------------------------------------------------------------- sovereign clouds

@description('ORQENTIS_AUTHORITY_HOST: Microsoft Entra authority host of a sovereign cloud, for example https://login.microsoftonline.us (US Government) or https://login.chinacloudapi.cn (China). Empty: the public cloud default.')
param authorityHost string = ''

@description('ORQENTIS_FABRIC_API_BASE_URL. Empty: https://api.fabric.microsoft.com/v1 (public cloud).')
param fabricApiBaseUrl string = ''

@description('ORQENTIS_POWERBI_API_BASE_URL. Empty: https://api.powerbi.com/v1.0/myorg (public cloud).')
param powerbiApiBaseUrl string = ''

@description('ORQENTIS_FABRIC_SCOPE: token scope of the Fabric API, ending in /.default. Empty: the public cloud scope.')
param fabricScope string = ''

@description('ORQENTIS_POWERBI_SCOPE: token scope of the Power BI API, ending in /.default. Empty: the public cloud scope.')
param powerbiScope string = ''

// ------------------------------------------------------------------------------------------------- data + logs

@description('Log Analytics retention in days.')
@minValue(30)
@maxValue(730)
param logRetentionInDays int = 30

@description('Storage redundancy for the MVP data share. Use Standard_ZRS where the region supports it.')
@allowed([
  'Standard_LRS'
  'Standard_ZRS'
])
param storageSkuName string = 'Standard_LRS'

@description('Quota (GiB) of the Azure Files share mounted at /var/lib/orqentis.')
@minValue(1)
@maxValue(5120)
param fileShareQuotaGiB int = 32

@description('Log Analytics daily ingestion cap in GB (-1: no cap). A cap keeps a sandbox cheap, but once it is reached nothing is ingested until the next day, including the metrics the alerts read: the heartbeat alert then fires. Use it in dev, not in prod (docs/cost.md).')
@minValue(-1)
@maxValue(1000)
param logDailyQuotaGb int = -1

// ------------------------------------------------------------------------------------------------- monitoring

@description('Deploy the action group, the six alert rules and the workbook (modules/monitoring.bicep). Alert rules cost a little (docs/cost.md); turn off only for throwaway environments.')
param monitoringEnabled bool = true

@description('Email addresses that receive every alert. Parameter files must not hold real people: the env/*.bicepparam files read them from the ALERT_EMAIL_RECEIVERS environment variable (comma separated) at deployment time. Empty: the action group exists but notifies nobody by email.')
param alertEmailReceivers array = []

@description('Optional Microsoft Teams Workflows webhook URL for the action group. It is a secret (it carries its own signature): pass it at deployment time (the env/*.bicepparam files read ALERT_TEAMS_WEBHOOK_URI), never commit it. Empty: no webhook receiver. Note that a deployment without it removes a receiver added earlier, because the template owns the action group.')
@secure()
param alertTeamsWebhookUri string = ''

@description('Base URL of docs/operations.md; every alert links to one section of it.')
param alertRunbookBaseUrl string = 'https://github.com/mjtpena/fabricops-digital-agent/blob/main/docs/operations.md'

@description('Minutes between evaluations of the log alert rules. Every log alert window below must be at least this long: the deployment is refused, naming the windows that are too short, otherwise.')
@allowed([
  1
  5
  10
  15
  30
  45
  60
])
param alertEvaluationFrequencyMinutes int = 5

@description('app-down: minutes without a running replica before the alert fires.')
@allowed([
  1
  5
  15
  30
  60
])
param alertAppDownWindowMinutes int = 5

@description('restart-loop: window in minutes in which restarts are counted. Azure accepts only these log alert windows: 5, 10, 15, 30, 45, 60, 120, 180, 240, 300, 360, 1440 or 2880 minutes.')
@allowed([
  5
  10
  15
  30
  45
  60
  120
  180
  240
  300
  360
  1440
  2880
])
param alertRestartWindowMinutes int = 15

@description('restart-loop: alert at this many restarts in the window. One deploy is one restart.')
@minValue(2)
@maxValue(100)
param alertRestartThreshold int = 3

@description('5xx: window in minutes over which the share of 5xx responses is computed (a log alert window, see alertRestartWindowMinutes).')
@allowed([
  5
  10
  15
  30
  45
  60
  120
  180
  240
  300
  360
  1440
  2880
])
param alertHttp5xxWindowMinutes int = 10

@description('5xx: alert when at least this percentage of the requests in the window answered 5xx.')
@minValue(1)
@maxValue(100)
param alertHttp5xxRatePercent int = 5

@description('5xx: ignore windows with fewer requests than this.')
@minValue(1)
@maxValue(100000)
param alertHttp5xxMinRequests int = 20

@description('collector-errors: window in minutes in which failing collector runs are counted (a log alert window, see alertRestartWindowMinutes).')
@allowed([
  5
  10
  15
  30
  45
  60
  120
  180
  240
  300
  360
  1440
  2880
])
param alertCollectorErrorWindowMinutes int = 15

@description('collector-errors: alert when one collector had at least this many failing runs in the window.')
@minValue(1)
@maxValue(1000)
param alertCollectorErrorRuns int = 2

@description('heartbeat: alert when there was no successful collection for this many minutes (a log alert window, see alertRestartWindowMinutes). The alert is switched off automatically while schedulerEnabled or collectEnabled is false.')
@allowed([
  5
  10
  15
  30
  45
  60
  120
  180
  240
  300
  360
  1440
  2880
])
param alertHeartbeatMinutes int = 15

@description('audit-verification: window in minutes in which a failed verification is visible to the rule. 0 (the default) derives it from auditVerifyIntervalMinutes: at least twice the interval, rounded up to the next log alert window (120 for the default hourly verification). Otherwise one of the log alert windows (see alertRestartWindowMinutes).')
@allowed([
  0
  5
  10
  15
  30
  45
  60
  120
  180
  240
  300
  360
  1440
  2880
])
param alertAuditFailureWindowMinutes int = 0

// ================================================================================================= variables

var readerIdentityName = 'id-${prefix}-reader'
var actorIdentityName = 'id-${prefix}-actor'
var containerAppName = 'ca-${prefix}'
var storageAccountName = take(toLower('st${replace(prefix, '-', '')}${uniqueString(resourceGroup().id)}'), 24)
var fileShareName = 'orqentis-data'
var dataMountPath = '/var/lib/orqentis'
var appPort = 8000

// Must match APP_UID / APP_GID in the Dockerfile: SMB mounts have no POSIX ownership, so the mount maps every
// file to the non-root app user. Owner-only modes keep the share private to that user inside the container.
// "nobrl" stops the CIFS client forwarding byte-range locks to the server, which SQLite cannot use over SMB
// ("database is locked"). It is only acceptable because exactly one process writes (one replica, see scale).
var dataMountOptions = 'uid=10001,gid=10001,dir_mode=0700,file_mode=0600,mfsymlinks,nobrl'

var registryServer = !empty(containerRegistryName)
  ? '${containerRegistryName}${environment().suffixes.acrLoginServer}'
  : containerRegistryServer

// Built-in role: AcrPull.
var acrPullRoleId = '7f951dda-4ed3-4680-a7ca-43fe172d538d'

// Placeholder keeps the indexes valid when azureOpenAiResourceId is empty (the module is then skipped).
var openAiIdSegments = split(
  empty(azureOpenAiResourceId)
    ? '/subscriptions/${subscription().subscriptionId}/resourceGroups/${resourceGroup().name}/providers/Microsoft.CognitiveServices/accounts/none'
    : azureOpenAiResourceId,
  '/'
)

// Same placeholder approach for containerRegistryResourceId (the module is then skipped).
var acrIdSegments = split(
  empty(containerRegistryResourceId)
    ? '/subscriptions/${subscription().subscriptionId}/resourceGroups/${resourceGroup().name}/providers/Microsoft.ContainerRegistry/registries/none'
    : containerRegistryResourceId,
  '/'
)

var appInsightsSecretName = 'appinsights-connection-string'

// Service-principal mode for a Fabric tenant other than this deployment's: the credential is a Key Vault
// reference that Container Apps resolves with the reader identity. Nothing secret is a parameter or an output.
var servicePrincipalMode = fabricAuthMode == 'service_principal'
var fabricTenantIdEffective = empty(fabricTenantId) ? tenant().tenantId : fabricTenantId
var useClientSecret = servicePrincipalMode && !empty(fabricServicePrincipalSecretUri)
var useClientCertificate = servicePrincipalMode && !empty(fabricServicePrincipalCertificateUri)
var fabricSpSecretName = 'fabric-sp-client-secret'
var fabricSpCertificateName = 'fabric-sp-certificate'
var fabricSpCertificateMountPath = '/mnt/fabric-sp'
var fabricSpCertificateFile = 'service-principal.pem'

// Placeholder keeps the indexes valid when keyVaultResourceId is empty (the module is then skipped).
var keyVaultIdSegments = split(
  empty(keyVaultResourceId)
    ? '/subscriptions/${subscription().subscriptionId}/resourceGroups/${resourceGroup().name}/providers/Microsoft.KeyVault/vaults/none'
    : keyVaultResourceId,
  '/'
)

// A Key Vault secret URI https://<vault>.<dns-suffix>/secrets/<name>[/<version>] splits on '/' into
// ['https:', '', '<vault>.<dns-suffix>', 'secrets', '<name>'] (plus '<version>'). Segments are indexed only after
// the length is known: a malformed URI must reach the readable check below, not fail here with an index error.
var secretUriSegments = split(fabricServicePrincipalSecretUri, '/')
var certificateUriSegments = split(fabricServicePrincipalCertificateUri, '/')

// The secrets (by name) the reader identity must read; the Key Vault role is assigned on these, not on the vault.
var keyVaultSecretNames = concat(
  useClientSecret && length(secretUriSegments) > 4 ? [secretUriSegments[4]] : [],
  useClientCertificate && length(certificateUriSegments) > 4 ? [certificateUriSegments[4]] : []
)

// service_principal inputs checked when the deployment runs (see modules/input-check.bicep): 'ok', or what is wrong.
// Nested conditionals are evaluated lazily, so a parse of a URI only happens after the URI is known to be set.
var credentialUri = empty(fabricServicePrincipalSecretUri)
  ? fabricServicePrincipalCertificateUri
  : fabricServicePrincipalSecretUri
var credentialUriSegments = split(credentialUri, '/')
var credentialUriShape = 'the Key Vault credential URI must look like https://<vault-name>.<key-vault-dns-suffix>/secrets/<secret-name> (optionally followed by /<version>): the secrets endpoint, not keys or certificates'
var servicePrincipalCheck = !servicePrincipalMode
  ? 'ok'
  : empty(fabricServicePrincipalClientId)
      ? 'fabricAuthMode is service_principal, so fabricServicePrincipalClientId (the application id of the service principal in the Fabric tenant) must be set'
      : length(fabricTenantId) != 36
          ? 'fabricAuthMode is service_principal, so fabricTenantId must be set to the id (a GUID) of the Fabric tenant, not left empty'
          : empty(fabricServicePrincipalSecretUri) == empty(fabricServicePrincipalCertificateUri)
              ? 'fabricAuthMode is service_principal, so set exactly one of fabricServicePrincipalSecretUri and fabricServicePrincipalCertificateUri'
              : !startsWith(credentialUri, 'https://')
                  ? credentialUriShape
                  : length(credentialUriSegments) < 5 || length(credentialUriSegments) > 6
                      ? credentialUriShape
                      : credentialUriSegments[3] != 'secrets' || empty(credentialUriSegments[4])
                          ? credentialUriShape
                          : !contains(credentialUriSegments[2], '.') || startsWith(credentialUriSegments[2], '.')
                              ? credentialUriShape
                              : !empty(keyVaultResourceId) && !startsWith(credentialUriSegments[2], '${keyVaultIdSegments[8]}.')
                                  ? 'the Key Vault credential URI is not in the vault named by keyVaultResourceId'
                                  : 'ok'

var keyVaultSecrets = concat(
  useClientSecret
    ? [
        {
          name: fabricSpSecretName
          keyVaultUrl: fabricServicePrincipalSecretUri
          identity: readerIdentity.id
        }
      ]
    : [],
  useClientCertificate
    ? [
        {
          name: fabricSpCertificateName
          keyVaultUrl: fabricServicePrincipalCertificateUri
          identity: readerIdentity.id
        }
      ]
    : []
)

// Ingress mode: ingressMode, or the deprecated externalIngress mapped to it, or the default.
var legacyIngressMode = (externalIngress ?? false)
  ? (empty(infrastructureSubnetId) ? 'externalAuthenticated' : 'vnet')
  : 'internal'
var effectiveIngressMode = externalIngress == null ? (ingressMode ?? 'externalAuthenticated') : legacyIngressMode
// Fail the deployment instead of silently choosing an exposure that differs from what was asked for:
// * both ingressMode and the deprecated externalIngress are set (a stale externalIngress = true must not
//   override an explicit ingressMode = 'internal');
// * "vnet" without a subnet, or "externalAuthenticated" with one.
// The failure is an invalid subnet id fed into the environment (the Bicep language has no assert).
var ingressMisconfigured = (externalIngress != null && ingressMode != null) || (effectiveIngressMode == 'vnet' && empty(infrastructureSubnetId)) || (effectiveIngressMode == 'externalAuthenticated' && !empty(infrastructureSubnetId))
var environmentSubnetId = ingressMisconfigured
  ? 'INVALID-conflicting-ingressMode-externalIngress-infrastructureSubnetId-see-README-Network-exposure'
  : infrastructureSubnetId
var ipSecurityRestrictions = [
  for (range, index) in allowedIpRanges: {
    name: 'allow-${index}'
    description: 'Orqentis FabricOps: allowed client range ${index}'
    ipAddressRange: range
    action: 'Allow'
  }
]
// Easy Auth does not authenticate these paths. Nothing under them reads the caller's identity.
var anonymousPaths = anonymousHealthEndpoints ? ['/health', '/health/ready'] : []

var appEnv = concat(
  [
    { name: 'ORQENTIS_DEPLOYMENT_ENVIRONMENT', value: deploymentEnvironment }
    { name: 'ORQENTIS_TENANT_ID', value: fabricTenantIdEffective }
    { name: 'ORQENTIS_API_AUTH_MODE', value: 'easy_auth' }
    { name: 'ORQENTIS_AUTH_MODE', value: fabricAuthMode }
    { name: 'ORQENTIS_MANAGED_IDENTITY_CLIENT_ID', value: readerIdentity.properties.clientId }
    // azure-identity reads AZURE_CLIENT_ID; with two identities attached it must name the reader. In
    // service_principal mode it names the service principal in the Fabric tenant instead (the reader is still
    // named by ORQENTIS_MANAGED_IDENTITY_CLIENT_ID, which the LLM credential uses).
    {
      name: 'AZURE_CLIENT_ID'
      value: servicePrincipalMode ? fabricServicePrincipalClientId : readerIdentity.properties.clientId
    }
    { name: 'ORQENTIS_DEFAULT_TIMEZONE', value: defaultTimezone }
    { name: 'ORQENTIS_FABRIC_MODE', value: fabricMode }
    { name: 'ORQENTIS_CONTRACTS_PATH', value: contractsPath }
    { name: 'ORQENTIS_ENABLE_WRITE_ACTIONS', value: enableWriteActions ? 'true' : 'false' }
    { name: 'ORQENTIS_REASONER', value: reasoner }
    { name: 'ORQENTIS_DATA_DIR', value: dataMountPath }
    { name: 'ORQENTIS_LOG_FORMAT', value: 'json' }
    { name: 'ORQENTIS_SCHEDULER_ENABLED', value: schedulerEnabled ? 'true' : 'false' }
    { name: 'ORQENTIS_SCHEDULER_STARTUP_DELAY_SECONDS', value: string(schedulerStartupDelaySeconds) }
    { name: 'ORQENTIS_SCHEDULER_TICK_SECONDS', value: string(schedulerTickSeconds) }
    { name: 'ORQENTIS_SCHEDULER_LEASE_TTL_SECONDS', value: string(schedulerLeaseTtlSeconds) }
    { name: 'ORQENTIS_SCHEDULER_JOB_TIMEOUT_MINUTES', value: string(schedulerJobTimeoutMinutes) }
    { name: 'ORQENTIS_SCHEDULER_COLLECT_ENABLED', value: collectEnabled ? 'true' : 'false' }
    { name: 'ORQENTIS_SCHEDULER_COLLECT_INTERVAL_MINUTES', value: string(collectIntervalMinutes) }
    { name: 'ORQENTIS_SCHEDULER_AUTO_INVESTIGATE_ENABLED', value: autoInvestigateEnabled ? 'true' : 'false' }
    { name: 'ORQENTIS_SCHEDULER_AUTO_INVESTIGATE_INTERVAL_MINUTES', value: string(autoInvestigateIntervalMinutes) }
    { name: 'ORQENTIS_SCHEDULER_AUTO_INVESTIGATE_BATCH_SIZE', value: string(autoInvestigateBatchSize) }
    { name: 'ORQENTIS_SCHEDULER_AUTO_INVESTIGATE_MAX_ATTEMPTS', value: string(autoInvestigateMaxAttempts) }
    { name: 'ORQENTIS_SCHEDULER_SLA_SWEEP_ENABLED', value: slaSweepEnabled ? 'true' : 'false' }
    { name: 'ORQENTIS_SCHEDULER_SLA_SWEEP_INTERVAL_MINUTES', value: string(slaSweepIntervalMinutes) }
    { name: 'ORQENTIS_SCHEDULER_CATALOG_REFRESH_ENABLED', value: catalogRefreshEnabled ? 'true' : 'false' }
    { name: 'ORQENTIS_SCHEDULER_CATALOG_REFRESH_INTERVAL_MINUTES', value: string(catalogRefreshIntervalMinutes) }
    { name: 'ORQENTIS_SCHEDULER_AUDIT_VERIFY_ENABLED', value: auditVerifyEnabled ? 'true' : 'false' }
    { name: 'ORQENTIS_SCHEDULER_AUDIT_VERIFY_INTERVAL_MINUTES', value: string(auditVerifyIntervalMinutes) }
    { name: 'ORQENTIS_INVESTIGATION_MAX_CONCURRENCY', value: string(investigationMaxConcurrency) }
    { name: 'ORQENTIS_INVESTIGATION_TIMEOUT_SECONDS', value: string(investigationTimeoutSeconds) }
    { name: 'ORQENTIS_FABRIC_MAX_CONCURRENCY', value: string(fabricMaxConcurrency) }
    { name: 'ORQENTIS_FABRIC_RATE_PER_SECOND', value: string(fabricRatePerSecond) }
    { name: 'ORQENTIS_FABRIC_RATE_BURST', value: string(fabricRateBurst) }
    { name: 'ORQENTIS_FABRIC_JOB_INSTANCES_MAX_PAGES', value: string(fabricJobInstancesMaxPages) }
    { name: 'ORQENTIS_COLLECTION_MAX_LOOKBACK_HOURS', value: string(collectionMaxLookbackHours) }
    { name: 'ORQENTIS_COLLECTION_OVERLAP_MINUTES', value: string(collectionOverlapMinutes) }
    { name: 'ORQENTIS_STARTUP_GRACE_SECONDS', value: string(startupGraceSeconds) }
    { name: 'ORQENTIS_BUILD_RETRY_INITIAL_SECONDS', value: string(buildRetryInitialSeconds) }
    { name: 'ORQENTIS_BUILD_RETRY_MAX_SECONDS', value: string(buildRetryMaxSeconds) }
    { name: 'ORQENTIS_API_MAX_BODY_BYTES', value: string(apiMaxBodyBytes) }
    { name: 'ORQENTIS_API_RATE_LIMIT_MUTATING_PER_MINUTE', value: string(apiRateLimitMutatingPerMinute) }
    { name: 'ORQENTIS_API_RATE_LIMIT_EXPENSIVE_PER_MINUTE', value: string(apiRateLimitExpensivePerMinute) }
    { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', secretRef: appInsightsSecretName }
  ],
  empty(azureOpenAiEndpoint) ? [] : [{ name: 'ORQENTIS_AZURE_OPENAI_ENDPOINT', value: azureOpenAiEndpoint }],
  empty(azureOpenAiDeployment) ? [] : [{ name: 'ORQENTIS_AZURE_OPENAI_DEPLOYMENT', value: azureOpenAiDeployment }],
  empty(llmInputCostPerMillionTokens)
    ? []
    : [{ name: 'ORQENTIS_LLM_INPUT_COST_PER_MILLION_TOKENS', value: llmInputCostPerMillionTokens }],
  empty(llmOutputCostPerMillionTokens)
    ? []
    : [{ name: 'ORQENTIS_LLM_OUTPUT_COST_PER_MILLION_TOKENS', value: llmOutputCostPerMillionTokens }],
  empty(authorityHost) ? [] : [{ name: 'ORQENTIS_AUTHORITY_HOST', value: authorityHost }],
  empty(fabricApiBaseUrl) ? [] : [{ name: 'ORQENTIS_FABRIC_API_BASE_URL', value: fabricApiBaseUrl }],
  empty(powerbiApiBaseUrl) ? [] : [{ name: 'ORQENTIS_POWERBI_API_BASE_URL', value: powerbiApiBaseUrl }],
  empty(fabricScope) ? [] : [{ name: 'ORQENTIS_FABRIC_SCOPE', value: fabricScope }],
  empty(powerbiScope) ? [] : [{ name: 'ORQENTIS_POWERBI_SCOPE', value: powerbiScope }],
  // service_principal mode: the Fabric tenant, and the LLM keeps using the reader managed identity (Azure
  // OpenAI lives in this tenant, not in the Fabric one).
  servicePrincipalMode
    ? [
        { name: 'AZURE_TENANT_ID', value: fabricTenantIdEffective }
        { name: 'ORQENTIS_LLM_AUTH_MODE', value: 'managed_identity' }
      ]
    : [],
  useClientSecret ? [{ name: 'AZURE_CLIENT_SECRET', secretRef: fabricSpSecretName }] : [],
  useClientCertificate
    ? [{ name: 'AZURE_CLIENT_CERTIFICATE_PATH', value: '${fabricSpCertificateMountPath}/${fabricSpCertificateFile}' }]
    : []
)

// ================================================================================================= observability

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'log-${prefix}'
  location: location
  tags: tags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: logRetentionInDays
    workspaceCapping: {
      dailyQuotaGb: logDailyQuotaGb
    }
    features: {
      enableLogAccessUsingOnlyResourcePermissions: true
    }
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: 'appi-${prefix}'
  location: location
  tags: tags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logAnalytics.id
    IngestionMode: 'LogAnalytics'
    // The app exports with the connection string (azure-monitor-opentelemetry), so local auth stays enabled.
    DisableLocalAuth: false
  }
}

// ================================================================================================= identities

@description('READ capability boundary: the operating identity of the app.')
resource readerIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: readerIdentityName
  location: location
  tags: union(tags, {
    'orqentis-capability': 'read'
  })
}

@description('WRITE capability boundary: attached to the app only when enableWriteActions is true.')
resource actorIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: actorIdentityName
  location: location
  tags: union(tags, {
    'orqentis-capability': 'write'
  })
}

var appIdentities = union(
  {
    '${readerIdentity.id}': {}
  },
  enableWriteActions
    ? {
        '${actorIdentity.id}': {}
      }
    : {}
)

// ARM may evaluate names/ids of condition-false resources (dependsOn graph); keep them well-formed.
resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = if (!empty(containerRegistryName)) {
  name: empty(containerRegistryName) ? 'unused' : containerRegistryName
}

resource readerAcrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(containerRegistryName)) {
  name: guid(registry.id, readerIdentity.id, acrPullRoleId)
  scope: registry
  properties: {
    description: 'Orqentis FabricOps reader identity: pull the app image.'
    principalId: readerIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
  }
}

module readerOpenAiUser 'modules/openai-role.bicep' = if (!empty(azureOpenAiResourceId)) {
  name: 'orqentis-openai-role-${uniqueString(azureOpenAiResourceId, readerIdentityName)}'
  scope: resourceGroup(openAiIdSegments[2], openAiIdSegments[4])
  params: {
    accountName: openAiIdSegments[8]
    principalId: readerIdentity.properties.principalId
  }
}

// Fails the deployment with a readable reason when the service_principal inputs cannot work (always 'ok' otherwise).
module servicePrincipalInputCheck 'modules/input-check.bicep' = {
  name: 'orqentis-check-service-principal-inputs'
  params: {
    result: any(servicePrincipalCheck) // a literal-typed parameter on purpose: ARM, not the compiler, rejects it
  }
}

module readerKeyVaultSecretsUser 'modules/keyvault-role.bicep' = if (!empty(keyVaultResourceId) && !empty(keyVaultSecretNames)) {
  name: 'orqentis-keyvault-role-${uniqueString(keyVaultResourceId, readerIdentityName)}'
  scope: resourceGroup(keyVaultIdSegments[2], keyVaultIdSegments[4])
  params: {
    vaultName: keyVaultIdSegments[8]
    secretNames: keyVaultSecretNames
    principalId: readerIdentity.properties.principalId
  }
}

// AcrPull on a registry that lives elsewhere (the shared registry). The deployment name includes this resource
// group's id because every environment deploys this module into the registry's resource group.
// The deploying principal therefore needs, in the REGISTRY's resource group, Microsoft.Resources/deployments/write (this
// module is a nested deployment there) and, on the registry, Microsoft.Authorization/roleAssignments/write for AcrPull
// (docs/delivery.md, section 5). Without the first, the deployment fails with AuthorizationFailed before the app exists.
module readerSharedAcrPull 'modules/acr-pull.bicep' = if (!empty(containerRegistryResourceId)) {
  name: 'orqentis-acr-pull-${uniqueString(containerRegistryResourceId, resourceGroup().id, readerIdentityName)}'
  scope: resourceGroup(acrIdSegments[2], acrIdSegments[4])
  params: {
    registryName: acrIdSegments[8]
    principalId: readerIdentity.properties.principalId
  }
}

// ================================================================================================= storage (MVP)

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageAccountName
  location: location
  tags: tags
  sku: {
    name: storageSkuName
  }
  kind: 'StorageV2'
  properties: {
    accessTier: 'Hot'
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    allowBlobPublicAccess: false
    allowCrossTenantReplication: false
    defaultToOAuthAuthentication: true
    // Container Apps mounts Azure Files over SMB with the account key; disabling shared key access breaks the
    // mount. The key is read at deployment time into the environment's storage definition only.
    allowSharedKeyAccess: true
    publicNetworkAccess: 'Enabled'
    networkAcls: {
      defaultAction: 'Allow'
      bypass: 'AzureServices'
    }
  }
}

resource fileService 'Microsoft.Storage/storageAccounts/fileServices@2023-05-01' = {
  parent: storage
  name: 'default'
  properties: {
    shareDeleteRetentionPolicy: {
      enabled: true
      days: 14
    }
  }
}

resource dataShare 'Microsoft.Storage/storageAccounts/fileServices/shares@2023-05-01' = {
  parent: fileService
  name: fileShareName
  properties: {
    accessTier: 'TransactionOptimized'
    enabledProtocols: 'SMB'
    shareQuota: fileShareQuotaGiB
  }
}

// ================================================================================================= container apps

resource managedEnvironment 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: 'cae-${prefix}'
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalytics.properties.customerId
        sharedKey: logAnalytics.listKeys().primarySharedKey
      }
    }
    workloadProfiles: [
      {
        name: 'Consumption'
        workloadProfileType: 'Consumption'
      }
    ]
    vnetConfiguration: empty(environmentSubnetId)
      ? null
      : {
          infrastructureSubnetId: environmentSubnetId
          internal: true
        }
    zoneRedundant: false
  }
}

resource environmentStorage 'Microsoft.App/managedEnvironments/storages@2024-03-01' = {
  parent: managedEnvironment
  name: fileShareName
  properties: {
    azureFile: {
      accountName: storage.name
      accountKey: storage.listKeys().keys[0].value
      shareName: dataShare.name
      accessMode: 'ReadWrite'
    }
  }
}

resource containerApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: containerAppName
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: appIdentities
  }
  properties: {
    environmentId: managedEnvironment.id
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: effectiveIngressMode != 'internal'
        targetPort: appPort
        transport: 'http'
        allowInsecure: false
        ipSecurityRestrictions: ipSecurityRestrictions
        traffic: [
          {
            latestRevision: true
            weight: 100
          }
        ]
      }
      registries: empty(registryServer)
        ? []
        : [
            {
              server: registryServer
              identity: readerIdentity.id
            }
          ]
      secrets: concat(
        [
          {
            name: appInsightsSecretName
            value: appInsights.properties.ConnectionString
          }
        ],
        keyVaultSecrets
      )
    }
    template: {
      containers: [
        {
          name: 'orqentis'
          image: containerImage
          resources: {
            cpu: json(containerCpu)
            memory: containerMemory
          }
          env: appEnv
          // /health is liveness: cheap, no dependencies, 200 as soon as the process listens (even while the container
          // is still being built, or after a build failure that is being retried). /health/ready is readiness: 503
          // until the container is built and the estate catalog is loaded. Reading the estate (rest mode) happens in
          // the background after the port opens, so it never counts against the startup probe.
          probes: [
            {
              type: 'Startup'
              httpGet: {
                path: '/health'
                port: appPort
                scheme: 'HTTP'
              }
              initialDelaySeconds: 5
              periodSeconds: 5
              timeoutSeconds: 3
              failureThreshold: 10
            }
            {
              type: 'Liveness'
              httpGet: {
                path: '/health'
                port: appPort
                scheme: 'HTTP'
              }
              periodSeconds: 30
              timeoutSeconds: 5
              failureThreshold: 3
            }
            {
              type: 'Readiness'
              httpGet: {
                path: '/health/ready'
                port: appPort
                scheme: 'HTTP'
              }
              periodSeconds: 10
              timeoutSeconds: 5
              failureThreshold: 3
            }
          ]
          volumeMounts: concat(
            [
              {
                volumeName: 'orqentis-data'
                mountPath: dataMountPath
              }
            ],
            useClientCertificate
              ? [
                  {
                    volumeName: fabricSpCertificateName
                    mountPath: fabricSpCertificateMountPath
                  }
                ]
              : []
          )
        }
      ]
      // SQLite (event store, state DB) and the hash-chained audit log allow a single writer only, and the leader
      // lease that gates the scheduler is in-process. Do not raise maxReplicas until the stores and the lease move
      // to the PostgreSQL system of record.
      scale: {
        minReplicas: 1
        maxReplicas: 1
      }
      volumes: concat(
        [
          {
            name: 'orqentis-data'
            storageType: 'AzureFile'
            storageName: environmentStorage.name
            mountOptions: dataMountOptions
          }
        ],
        useClientCertificate
          ? [
              {
                // The Key Vault secret (PEM) as a read-only file; the private key never touches the share.
                name: fabricSpCertificateName
                storageType: 'Secret'
                secrets: [
                  {
                    secretRef: fabricSpCertificateName
                    path: fabricSpCertificateFile
                  }
                ]
              }
            ]
          : []
      )
    }
  }
  dependsOn: [
    readerAcrPull
    readerSharedAcrPull
    readerKeyVaultSecretsUser
    servicePrincipalInputCheck
  ]
}

// Container Apps built-in authentication (Easy Auth) with Microsoft Entra ID. Unauthenticated requests get 401;
// authenticated requests reach the app with X-MS-CLIENT-PRINCIPAL (claims, including app roles). Easy Auth strips any
// client-supplied copy of that header. The app classifies the caller from those claims (app-only tokens are services
// and can never approve), which is why the app registration must emit the idtyp optional claim (README step 1).
// No client secret: the API only validates bearer tokens, it never runs an interactive login flow.
// Authentication is required on every path except the anonymousPaths above (health only, when
// anonymousHealthEndpoints is true). Container Apps has no separate "requireAuthentication" flag, unlike App Service.
// Container health probes go straight to the container port and do not pass through this front end.
resource authConfig 'Microsoft.App/containerApps/authConfigs@2024-03-01' = {
  parent: containerApp
  name: 'current'
  properties: {
    platform: {
      enabled: true
    }
    globalValidation: {
      unauthenticatedClientAction: 'Return401'
      excludedPaths: anonymousPaths
    }
    httpSettings: {
      requireHttps: true
    }
    identityProviders: {
      azureActiveDirectory: {
        enabled: true
        registration: {
          clientId: entraClientId
          openIdIssuer: '${environment().authentication.loginEndpoint}${entraTenantId}/v2.0'
        }
        validation: {
          allowedAudiences: [
            entraClientId
            'api://${entraClientId}'
          ]
          defaultAuthorizationPolicy: empty(allowedClientApplications)
            ? null
            : {
                allowedApplications: allowedClientApplications
              }
        }
      }
    }
  }
}

// ================================================================================================= monitoring

// Action group, six alert rules and the workbook (WS9, ADR-0019). Everything is derived from the resources above.
module monitoring 'modules/monitoring.bicep' = if (monitoringEnabled) {
  name: 'orqentis-monitoring'
  params: {
    location: location
    prefix: prefix
    tags: tags
    logAnalyticsWorkspaceId: logAnalytics.id
    containerAppId: containerApp.id
    containerAppName: containerApp.name
    emailReceivers: alertEmailReceivers
    teamsWebhookUri: alertTeamsWebhookUri
    runbookBaseUrl: alertRunbookBaseUrl
    evaluationFrequencyMinutes: alertEvaluationFrequencyMinutes
    appDownWindowMinutes: alertAppDownWindowMinutes
    restartWindowMinutes: alertRestartWindowMinutes
    restartThreshold: alertRestartThreshold
    http5xxWindowMinutes: alertHttp5xxWindowMinutes
    http5xxRatePercent: alertHttp5xxRatePercent
    http5xxMinRequests: alertHttp5xxMinRequests
    collectorErrorWindowMinutes: alertCollectorErrorWindowMinutes
    collectorErrorRuns: alertCollectorErrorRuns
    heartbeatMinutes: alertHeartbeatMinutes
    // A deliberately paused scheduler (or collect job) must not page anyone for a missing heartbeat.
    heartbeatAlertEnabled: schedulerEnabled && collectEnabled
    auditFailureWindowMinutes: alertAuditFailureWindowMinutes
    auditVerifyIntervalMinutes: auditVerifyIntervalMinutes
  }
}

// ================================================================================================= outputs

@description('Name of the Container App.')
output containerAppName string = containerApp.name

@description('Effective ingress mode: externalAuthenticated, vnet or internal.')
output ingressModeEffective string = effectiveIngressMode

@description('Ingress FQDN of the Container App (reachable as described by ingressModeEffective).')
output containerAppFqdn string = containerApp.properties.configuration.ingress.fqdn

@description('Reader (READ) identity: grant Fabric workspace Viewer to this principal id (see README).')
output readerIdentityPrincipalId string = readerIdentity.properties.principalId

@description('Reader (READ) identity client id (ORQENTIS_MANAGED_IDENTITY_CLIENT_ID / AZURE_CLIENT_ID).')
output readerIdentityClientId string = readerIdentity.properties.clientId

@description('Reader (READ) identity resource id.')
output readerIdentityResourceId string = readerIdentity.id

@description('Actor (WRITE) identity principal id: grant Fabric Contributor only on workspaces where writes are permitted.')
output actorIdentityPrincipalId string = actorIdentity.properties.principalId

@description('Actor (WRITE) identity client id.')
output actorIdentityClientId string = actorIdentity.properties.clientId

@description('Actor (WRITE) identity resource id.')
output actorIdentityResourceId string = actorIdentity.id

@description('Whether the actor (WRITE) identity is attached to the Container App.')
output actorIdentityAttached bool = enableWriteActions

@description('Log Analytics workspace resource id.')
output logAnalyticsWorkspaceId string = logAnalytics.id

@description('Application Insights resource name (the connection string is only stored as a Container App secret).')
output applicationInsightsName string = appInsights.name

@description('Storage account holding the MVP data share.')
output storageAccountName string = storage.name

@description('Azure Files share mounted at /var/lib/orqentis.')
output dataFileShareName string = dataShare.name

@description('Resource id of the alert action group (empty when monitoringEnabled is false).')
output alertActionGroupId string = monitoring.?outputs.actionGroupId ?? ''

@description('Names of the alert rules (empty when monitoringEnabled is false).')
output alertRuleNames array = monitoring.?outputs.alertRuleNames ?? []

@description('Email receivers on the action group; 0 means alerts notify nobody by email.')
output alertEmailReceiverCount int = monitoring.?outputs.emailReceiverCount ?? 0
