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
//   * Azure Database for PostgreSQL Flexible Server (Microsoft Entra authentication only, no password): the system
//     of record for incidents, approvals, events, scheduler leases and the hash-chained audit trail
//     (modules/postgres.bicep; ADR-0008). The app is stateless and runs as two or more replicas.
//   * Storage account for evidence, reports and audit anchors (Blob, Entra only, version-level immutability), with
//     data access for the reader identity on its two containers only (modules/evidence-storage.bicep).
//   * Container Apps environment (logs to Log Analytics) and the FabricOps Container App, protected by
//     Container Apps built-in authentication (Microsoft Entra ID). The app trusts X-MS-CLIENT-PRINCIPAL,
//     which only that front end can set. Exposure is chosen with ingressMode (default: external and
//     authenticated; see README "Network exposure", including the safe first deployment).
//   * Optional: Cognitive Services OpenAI User for the reader identity on an existing Azure OpenAI account.
//   * Notifications (Teams Workflows or a generic webhook) and the server-rendered web UI under /ui, with optional
//     interactive Easy Auth sign-in. Webhook URLs and the sign-in client secret are Key Vault references only.
//
// No credentials are written anywhere: identities are managed, PostgreSQL accepts Entra tokens only, storage Shared
// Key access is off, and the Application Insights connection string is a Container App secret. (The Log Analytics
// key for the environment's log destination is the only key read at deployment time.)

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

@description('ORQENTIS_CONTRACTS_CONTAINER: the Blob container, in the evidence storage account, that holds the estate contracts file (declared topology, data products and SLAs; required in rest mode). The template creates it and gives the reader identity read access to it; whoever uploads the file needs Storage Blob Data Contributor on this container (infra/bicep/README.md, Estate contracts). A rest-mode app stays not ready (/health/ready answers 503) until a valid file is in it.')
@minLength(3)
@maxLength(63)
param contractsContainerName string = 'contracts'

@description('ORQENTIS_CONTRACTS_BLOB_NAME: name of the estate contracts blob in contractsContainerName.')
@minLength(1)
@maxLength(1024)
param contractsBlobName string = 'estate.json'

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

@description('Seconds the scheduler waits after the app starts before it takes the leader lease or runs a job (ORQENTIS_SCHEDULER_STARTUP_DELAY_SECONDS). Container Apps runs the old and the new revision side by side during a deploy; the database lease already guarantees that only one process runs the jobs, so this delay is only a courtesy: a starting revision does not compete for the lease while it is still warming up.')
@minValue(0)
@maxValue(900)
param schedulerStartupDelaySeconds int = 30

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

@description('Anchor the audit chain head (sequence and hash) to an immutable blob on a schedule (ORQENTIS_SCHEDULER_AUDIT_ANCHOR_ENABLED). The anchor is what makes records lost in a database restore, or rewritten in the database, detectable.')
param auditAnchorEnabled bool = true

@description('Minutes between audit anchors (ORQENTIS_SCHEDULER_AUDIT_ANCHOR_INTERVAL_MINUTES). Audit records written after the latest anchor are not covered by it, so this interval bounds the window in which a loss would go unproven.')
@minValue(1)
@maxValue(1440)
param auditAnchorIntervalMinutes int = 15

@description('Seconds between polls for queued investigation jobs; every replica polls (ORQENTIS_INVESTIGATION_POLL_INTERVAL_SECONDS).')
@minValue(1)
@maxValue(60)
param investigationPollIntervalSeconds int = 2

@description('Seconds after which an investigation claim that was not renewed, because its replica crashed, is released for another replica (ORQENTIS_INVESTIGATION_CLAIM_TTL_SECONDS). Must be at least three times investigationPollIntervalSeconds, or the deployment fails.')
@minValue(10)
@maxValue(3600)
param investigationClaimTtlSeconds int = 120

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

@description('Optional: resource id of a subnet (delegated to Microsoft.App/environments, /27 or larger) that VNet-integrates the Container Apps environment. With ingressMode "vnet" (required) or "internal" (optional) the environment is internal, with no public endpoint. With "externalAuthenticated" the environment is VNet-integrated but keeps its public, authenticated endpoint: outbound traffic leaves through the VNet, which is what lets the app reach a VNet-integrated PostgreSQL server privately. test and prod must set it (the deployment fails otherwise).')
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

// ------------------------------------------------------------------------------------------------- notifications + web UI

@description('ORQENTIS_NOTIFIER: where notifications go. "null": nowhere (default). "teams_workflows": a Microsoft Teams Workflows webhook (needs notifyTeamsWebhookSecretUri). "webhook": a generic JSON webhook (needs notifyWebhookSecretUri). The URLs are secrets and exist only as Key Vault references; there is deliberately no parameter that takes a URL itself.')
@allowed([
  'null'
  'teams_workflows'
  'webhook'
])
param notifierKind string = 'null'

@description('notifierKind = teams_workflows: URI of the Key Vault SECRET that holds the Workflows webhook URL, for example https://<vault>.vault.azure.net/secrets/teams-webhook-url (no version, so a rotated URL is picked up). Resolved by Container Apps with the reader identity and exposed to the app as ORQENTIS_NOTIFY_TEAMS_WEBHOOK_URL. See docs/teams-notifications.md.')
param notifyTeamsWebhookSecretUri string = ''

@description('notifierKind = webhook: URI of the Key Vault secret that holds the webhook URL (ORQENTIS_NOTIFY_WEBHOOK_URL). Same rules as notifyTeamsWebhookSecretUri.')
param notifyWebhookSecretUri string = ''

@description('notifierKind = webhook, optional: URI of the Key Vault secret that holds the HMAC-SHA256 signing key (ORQENTIS_NOTIFY_WEBHOOK_SIGNING_SECRET). The receiver verifies X-Orqentis-Signature with it.')
param notifyWebhookSigningSecretUri string = ''

@description('ORQENTIS_NOTIFY_ALLOWED_HOSTS: comma-separated host patterns a webhook URL may use ("hooks.example.com", "*.logic.azure.com"). Empty: the Power Platform / Logic Apps hosts for teams_workflows, no restriction for webhook. Private, loopback, link-local and metadata addresses are refused after DNS resolution whatever this says.')
param notifyAllowedHosts string = ''

@description('ORQENTIS_PUBLIC_BASE_URL: the origin people use to reach the app, for example https://fabricops.example.com, without a path. Notification deep links are built from it and it is the second origin the CSRF check accepts. Empty: derived from the Container Apps environment (https://<app>.<environment domain>); set it when a custom domain or Front Door is in front.')
param publicBaseUrl string = ''

@description('ORQENTIS_UI_ENABLED: serve the server-rendered web UI under /ui (incidents, reports, approvals, audit verification). It uses the same authorisation guards and services as the API.')
param uiEnabled bool = true

@description('Interactive browser sign-in for the UI: URI of the Key Vault secret that holds the CLIENT SECRET of the Entra app registration (entraClientId). Empty (default): no interactive sign-in is configured, as before, and the UI is reachable only with a bearer token. When set, Easy Auth runs the authorization code flow at /.auth/login/aad and notification links go through it. Container Apps authentication does not support a managed-identity federated credential instead of the secret (infra/bicep/README.md, "Interactive sign-in for the UI").')
param uiSignInClientSecretUri string = ''

@description('ORQENTIS_UI_CSRF_SECRET: URI of the Key Vault secret that holds the HMAC key of the form tokens. Empty: a random key per process, which is correct for one replica. Required before running more than one replica, or a form rendered by one replica is refused by another.')
param uiCsrfSecretUri string = ''

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

@description('Storage redundancy of the evidence storage account (evidence, reports, audit anchors). Use Standard_ZRS where the region supports it.')
@allowed([
  'Standard_LRS'
  'Standard_ZRS'
])
param storageSkuName string = 'Standard_LRS'

// ------------------------------------------------------------------------------------------------- scale

@description('Container App minimum replicas. The app is stateless: the audit chain, the leader lease, approvals and investigation jobs are serialised by the database, so two replicas (the default) give availability and zero-downtime deploys. dev uses 1.')
@minValue(1)
@maxValue(30)
param minReplicas int = 2

@description('Container App maximum replicas; at least minReplicas. Every replica opens up to postgresPoolMax database connections, see postgresPoolMax.')
@minValue(1)
@maxValue(30)
param maxReplicas int = 2

// ------------------------------------------------------------------------------------------------- durable storage: PostgreSQL

@description('ORQENTIS_POSTGRES_POOL_MAX: database connections per replica (at least 2: one is held by the schema migration lock). During a deploy the old and the new revision can both run at maxReplicas, so the budget is 2 x maxReplicas x postgresPoolMax plus 15 connections the platform reserves plus 5 for the migration and operators (psql, restore checks); keep it at or below the server\'s max_connections (about 50 on Standard_B1ms). The deployment checks that SKU and fails when the budget is exceeded; for other SKUs check SHOW max_connections.')
@minValue(2)
@maxValue(100)
param postgresPoolMax int = 6

@description('Compute SKU of the PostgreSQL Flexible Server. Standard_B* is Burstable (dev, test), Standard_D* General Purpose (prod); the tier is derived from the name.')
param postgresSkuName string = 'Standard_B1ms'

@description('Storage (GiB) of the PostgreSQL server; it auto-grows. After it has grown, raise this value to the current size before the next deployment: a smaller value is refused.')
@allowed([
  32
  64
  128
  256
  512
  1024
  2048
  4096
  8192
  16384
  32767
])
param postgresStorageSizeGiB int = 32

@description('Major PostgreSQL version of a NEW server.')
@allowed([
  '14'
  '15'
  '16'
  '17'
])
param postgresVersion string = '17'

@description('Days of backups kept for point-in-time restore (7-35). The restore window of docs/operations/restore.md.')
@minValue(7)
@maxValue(35)
param postgresBackupRetentionDays int = 7

@description('Copy PostgreSQL backups to the paired region (geo-restore). Chosen when the server is created; it also moves backups across a regional boundary, which is a data-residency decision.')
param postgresGeoRedundantBackup bool = false

@description('PostgreSQL high availability: Disabled, ZoneRedundant or SameZone. Not available on the Burstable tier (the deployment fails if you combine them).')
@allowed([
  'Disabled'
  'ZoneRedundant'
  'SameZone'
])
param postgresHighAvailabilityMode string = 'Disabled'

@description('REQUIRED. Object id of the Microsoft Entra principal that administers the PostgreSQL server: a group is recommended, so that people can be added without touching the server. It runs the bootstrap step (infra/bicep/README.md) that creates the app\'s database role. The deployment fails with a readable reason when this is empty.')
param postgresEntraAdminObjectId string

@description('REQUIRED. Name of that principal (for a group, its display name); it is the PostgreSQL role name people sign in with. The deployment fails when this is empty.')
param postgresEntraAdminName string

@description('Type of the PostgreSQL Entra administrator: Group (default), User or ServicePrincipal.')
@allowed([
  'Group'
  'User'
  'ServicePrincipal'
])
param postgresEntraAdminType string = 'Group'

@description('Name of the application database.')
@minLength(1)
@maxLength(63)
param postgresDatabaseName string = 'orqentis'

@description('Public access: firewall rules, an array of { name, startIpAddress, endIpAddress }. Empty (default) lets nobody connect from outside Azure; add the address of whoever runs the bootstrap step. Must be empty with VNet integration (postgresDelegatedSubnetId).')
param postgresAllowedIpRanges array = []

@description('Public access: add the 0.0.0.0-0.0.0.0 rule that lets Azure services, including Container Apps whose outbound addresses are not fixed, reach the TCP port. It opens the port to resources of EVERY Azure tenant (every connection still needs a valid Entra token for a database role). For dev only: the deployment fails outside dev. Default false; env/dev.bicepparam sets it. test and prod use private networking (postgresDelegatedSubnetId).')
param postgresAllowAzureServices bool = false

@description('VNet integration (required outside dev): resource id of a subnet delegated to Microsoft.DBforPostgreSQL/flexibleServers, not the Container Apps subnet. Needs postgresPrivateDnsZoneId, a VNet-integrated Container Apps environment (infrastructureSubnetId) in the same or a peered VNet, and no firewall rules; the server then has no public endpoint. Cannot be changed on an existing server.')
param postgresDelegatedSubnetId string = ''

@description('VNet integration (required outside dev): resource id of the private DNS zone, whose name ends in postgres.database.azure.com (for example <name>.private.postgres.database.azure.com), linked to the VNet.')
param postgresPrivateDnsZoneId string = ''

@description('Empty (default): this template creates the PostgreSQL server. Set to the name of a server in THIS resource group (for example one restored from backup, docs/operations/restore.md) to point the app at it instead: nothing about that server is created or changed. The Entra administrator, firewall rules and parameters of an adopted server are yours to maintain.')
param postgresExistingServerName string = ''

// ------------------------------------------------------------------------------------------------- durable storage: evidence

@description('ORQENTIS_EVIDENCE_CONTAINER: container for evidence and incident reports.')
@minLength(3)
@maxLength(63)
param evidenceContainerName string = 'evidence'

@description('ORQENTIS_AUDIT_ANCHOR_CONTAINER: container for the audit-chain anchors.')
@minLength(3)
@maxLength(63)
param auditAnchorContainerName string = 'audit-anchors'

@description('ORQENTIS_AUDIT_EPOCH_CONTAINER: container for the audit epoch-start markers an operator writes after a restore. The app can only READ it (Storage Blob Data Reader), so a compromised app identity cannot open or close an epoch.')
@minLength(3)
@maxLength(63)
param auditEpochContainerName string = 'audit-epochs'

@description('Optional: object id of the Entra group of operators who may open an audit epoch after a restore (`orqentis audit start-epoch`). When set, the group gets Storage Blob Data Contributor on the audit-epochs container ONLY. Empty (default): the template assigns nothing and the owner grants it (infra/bicep/README.md, Storage). env files read it from AUDIT_OPERATORS_OBJECT_ID.')
param auditOperatorsPrincipalId string = ''

@description('Days every evidence and anchor blob version is immutable (default retention of the storage account).')
@minValue(1)
@maxValue(3650)
param evidenceRetentionDays int = 30

@description('Unlocked (default): the immutability policy can still be changed by an account owner. Locked: irreversible, retention can only be extended and the account cannot be deleted until every retention has expired. Locking needs the owner\'s approval; it can only be set on an account that already has the policy Unlocked (deploy Unlocked first).')
@allowed([
  'Unlocked'
  'Locked'
])
param evidenceImmutabilityState string = 'Unlocked'

@description('Days a deleted or overwritten evidence blob, and a deleted container, stays recoverable (soft delete).')
@minValue(1)
@maxValue(365)
param evidenceSoftDeleteDays int = 14

@description('Evidence storage network default action. Allow (default): reachable from the internet, every request needs an Entra token and a role. Deny: only evidenceAllowedIpRanges and evidenceAllowedSubnetIds can connect; a Container Apps environment without a VNet has no fixed address, so Deny needs ingressMode vnet and a subnet with the Microsoft.Storage service endpoint.')
@allowed([
  'Allow'
  'Deny'
])
param evidenceNetworkDefaultAction string = 'Allow'

@description('With evidenceNetworkDefaultAction Deny: IPv4 addresses or CIDR ranges allowed to connect (a single address has no mask).')
param evidenceAllowedIpRanges array = []

@description('With evidenceNetworkDefaultAction Deny: resource ids of subnets (with the Microsoft.Storage service endpoint) allowed to connect.')
param evidenceAllowedSubnetIds array = []

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
// "stev" marks the evidence account; it differs from the account name of earlier (Azure Files) versions of this
// template, so an environment that ran those gets a NEW account (version-level immutability can only be switched on
// when an account is created).
var evidenceStorageAccountName = take(toLower('stev${replace(prefix, '-', '')}${uniqueString(resourceGroup().id)}'), 24)
var postgresServerName = toLower('psql-${prefix}-${uniqueString(resourceGroup().id)}')
// The container's own writable directory (ephemeral, per replica). Nothing durable is kept there: the system of
// record is PostgreSQL and the evidence is in Blob storage.
var dataDir = '/var/lib/orqentis'
var appPort = 8000

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
  useClientCertificate && length(certificateUriSegments) > 4 ? [certificateUriSegments[4]] : [],
  map(configuredVaultReferences, s => length(split(s.uri, '/')) > 4 ? split(s.uri, '/')[4] : '')
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

// Notifications and the web UI (ADR-0018). Every secret is a Key Vault reference that Container Apps resolves with the
// reader identity; nothing secret is a parameter value or an output.
var uiSignInSecretName = 'microsoft-provider-authentication-secret'
var uiSignInEnabled = !empty(uiSignInClientSecretUri)
var uiSignInPath = '/.auth/login/aad'
var vaultReferences = [
  { secretName: 'notify-teams-webhook-url', envName: 'ORQENTIS_NOTIFY_TEAMS_WEBHOOK_URL', uri: notifyTeamsWebhookSecretUri }
  { secretName: 'notify-webhook-url', envName: 'ORQENTIS_NOTIFY_WEBHOOK_URL', uri: notifyWebhookSecretUri }
  { secretName: 'notify-webhook-signing-secret', envName: 'ORQENTIS_NOTIFY_WEBHOOK_SIGNING_SECRET', uri: notifyWebhookSigningSecretUri }
  { secretName: 'ui-csrf-secret', envName: 'ORQENTIS_UI_CSRF_SECRET', uri: uiCsrfSecretUri }
  { secretName: uiSignInSecretName, envName: '', uri: uiSignInClientSecretUri }
]
var configuredVaultReferences = filter(vaultReferences, s => !empty(s.uri))
var envVaultReferences = filter(configuredVaultReferences, s => !empty(s.envName))

// The origin of the app: the parameter, else the default domain of the environment. An app with internal-only ingress
// has ".internal" in its name.
var publicBaseUrlEffective = empty(publicBaseUrl)
  ? 'https://${containerAppName}.${effectiveIngressMode == 'internal' ? 'internal.' : ''}${managedEnvironment.properties.defaultDomain}'
  : publicBaseUrl

// The notification / sign-in inputs checked when the deployment runs (see modules/input-check.bicep): 'ok', or what is wrong.
var misshapenVaultReferences = filter(
  configuredVaultReferences,
  s => !startsWith(s.uri, 'https://')
    ? true
    : length(split(s.uri, '/')) != 5
        ? true
        : split(s.uri, '/')[3] != 'secrets'
            ? true
            : empty(split(s.uri, '/')[4]) ? true : !contains(split(s.uri, '/')[2], '.') ? true : false
)
var foreignVaultReferences = empty(keyVaultResourceId)
  ? []
  : filter(
      configuredVaultReferences,
      s => length(split(s.uri, '/')) == 5 ? !startsWith(split(s.uri, '/')[2], '${keyVaultIdSegments[8]}.') : false
    )
var notificationCheck = !empty(misshapenVaultReferences)
  ? 'the Key Vault URI for ${misshapenVaultReferences[0].secretName} must look like https://<vault-name>.<key-vault-dns-suffix>/secrets/<secret-name> (no version, no trailing slash): the secrets endpoint, not keys or certificates'
  : !empty(foreignVaultReferences)
      ? 'the Key Vault URI for ${foreignVaultReferences[0].secretName} is not in the vault named by keyVaultResourceId'
      : (notifierKind == 'teams_workflows' && empty(notifyTeamsWebhookSecretUri))
          ? 'notifierKind is teams_workflows, so notifyTeamsWebhookSecretUri (the Key Vault secret that holds the Workflows webhook URL) must be set'
          : (notifierKind == 'webhook' && empty(notifyWebhookSecretUri))
              ? 'notifierKind is webhook, so notifyWebhookSecretUri (the Key Vault secret that holds the webhook URL) must be set'
              : (!empty(publicBaseUrl) && (!startsWith(publicBaseUrl, 'https://') || length(split(publicBaseUrl, '/')) != 3))
                  ? 'publicBaseUrl must be an https origin such as https://fabricops.example.com (no path, no trailing slash)'
                  : 'ok'

// Ingress mode: ingressMode, or the deprecated externalIngress mapped to it, or the default.
var legacyIngressMode = (externalIngress ?? false)
  ? (empty(infrastructureSubnetId) ? 'externalAuthenticated' : 'vnet')
  : 'internal'
var effectiveIngressMode = externalIngress == null ? (ingressMode ?? 'externalAuthenticated') : legacyIngressMode
// Fail the deployment instead of silently choosing an exposure that differs from what was asked for:
// * both ingressMode and the deprecated externalIngress are set (a stale externalIngress = true must not
//   override an explicit ingressMode = 'internal');
// * "vnet" without a subnet. ("externalAuthenticated" with a subnet is valid: a VNet-integrated environment that
//   keeps its public, authenticated endpoint; only "vnet" and "internal" make the environment itself internal.)
// The failure is an invalid subnet id fed into the environment (the Bicep language has no assert).
var ingressMisconfigured = (externalIngress != null && ingressMode != null) || (effectiveIngressMode == 'vnet' && empty(infrastructureSubnetId))
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

// Durable storage inputs checked when the deployment runs (see modules/input-check.bicep): 'ok', or what is wrong.
// Connection budget. Standard_B1ms allows about 50 connections (max_connections). The worst case during a deploy is
// the old revision AND the new revision both running at maxReplicas (the new one scales up while the old one drains),
// each replica holding a full pool: 2 x maxReplicas x postgresPoolMax. On top of that: about 15 connections the
// platform reserves (replication, monitoring) and 5 for the schema migration and for operators (psql, restore checks).
// Other SKUs are not checked here: see SHOW max_connections.
var postgresPlatformConnections = 15
var postgresMigrationAndOperatorConnections = 5
var postgresConnectionsNeeded = 2 * maxReplicas * postgresPoolMax + postgresPlatformConnections + postgresMigrationAndOperatorConnections
var storageCheck = length(postgresEntraAdminObjectId) != 36
  ? 'postgresEntraAdminObjectId must be set to the object id (a GUID) of the Microsoft Entra group, user or service principal that administers PostgreSQL (pipeline variable POSTGRES_ENTRA_ADMIN_OBJECT_ID); it is empty or not a GUID'
  : empty(postgresEntraAdminName)
      ? 'postgresEntraAdminName must be set to the name of the Microsoft Entra principal that administers PostgreSQL (pipeline variable POSTGRES_ENTRA_ADMIN_NAME); it is empty'
      : maxReplicas < minReplicas
          ? 'maxReplicas must be at least minReplicas'
          : (deploymentEnvironment != 'dev' && (empty(postgresDelegatedSubnetId) || empty(postgresPrivateDnsZoneId)))
              ? 'deploymentEnvironment is ${deploymentEnvironment}: outside dev PostgreSQL must be private, so set postgresDelegatedSubnetId and postgresPrivateDnsZoneId (public access with a firewall is for dev only; infra/bicep/README.md, Private networking)'
              : (deploymentEnvironment != 'dev' && empty(infrastructureSubnetId))
                  ? 'deploymentEnvironment is ${deploymentEnvironment}: outside dev the Container Apps environment must be VNet-integrated to reach PostgreSQL privately, so set infrastructureSubnetId (a subnet delegated to Microsoft.App/environments)'
                  : (deploymentEnvironment != 'dev' && postgresAllowAzureServices)
                      ? 'deploymentEnvironment is ${deploymentEnvironment}: postgresAllowAzureServices opens the PostgreSQL port to every Azure tenant and is for dev only'
                      : (!empty(postgresDelegatedSubnetId) && postgresDelegatedSubnetId == infrastructureSubnetId)
                          ? 'postgresDelegatedSubnetId must not be the Container Apps subnet (infrastructureSubnetId): each needs its own subnet, with its own delegation'
                          : (!empty(auditOperatorsPrincipalId) && length(auditOperatorsPrincipalId) != 36)
                              ? 'auditOperatorsPrincipalId must be empty or the object id (a GUID) of the Entra group of audit operators'
                              : (!empty(postgresDelegatedSubnetId) != !empty(postgresPrivateDnsZoneId))
                  ? 'PostgreSQL VNet integration needs both postgresDelegatedSubnetId and postgresPrivateDnsZoneId, or neither'
                  : (!empty(postgresDelegatedSubnetId) && (!empty(postgresAllowedIpRanges) || postgresAllowAzureServices))
                      ? 'PostgreSQL VNet integration (postgresDelegatedSubnetId) has no public endpoint, so postgresAllowedIpRanges must be empty and postgresAllowAzureServices false'
                      : (postgresHighAvailabilityMode != 'Disabled' && startsWith(postgresSkuName, 'Standard_B'))
                          ? 'PostgreSQL high availability is not available on the Burstable tier (postgresSkuName Standard_B*): use a General Purpose SKU such as Standard_D2ds_v5 or set postgresHighAvailabilityMode to Disabled'
                          : (postgresSkuName == 'Standard_B1ms' && postgresConnectionsNeeded > 50)
                              ? 'Standard_B1ms allows about 50 connections but 2 x maxReplicas x postgresPoolMax (old and new revision both at maxReplicas) + 15 platform + 5 migration and operator connections is ${postgresConnectionsNeeded}: lower postgresPoolMax or maxReplicas, or use a larger postgresSkuName'
                              : investigationClaimTtlSeconds < investigationPollIntervalSeconds * 3
                                  ? 'investigationClaimTtlSeconds must be at least three times investigationPollIntervalSeconds'
                                  : (evidenceNetworkDefaultAction == 'Deny' && empty(evidenceAllowedIpRanges) && empty(evidenceAllowedSubnetIds))
                                      ? 'evidenceNetworkDefaultAction Deny with no evidenceAllowedIpRanges and no evidenceAllowedSubnetIds would lock the app out of its own evidence storage'
                                      : 'ok'

// The PostgreSQL role is the reader identity's NAME (the bootstrap step creates it); the connection authenticates with
// that identity's Entra token (ORQENTIS_MANAGED_IDENTITY_CLIENT_ID selects the identity). Nothing here is a credential.
var durableStorageEnv = [
  { name: 'ORQENTIS_STORAGE_BACKEND', value: 'postgres' }
  { name: 'ORQENTIS_POSTGRES_HOST', value: postgres.outputs.fqdn }
  { name: 'ORQENTIS_POSTGRES_DATABASE', value: postgres.outputs.databaseName }
  { name: 'ORQENTIS_POSTGRES_USER', value: readerIdentityName }
  { name: 'ORQENTIS_POSTGRES_POOL_MAX', value: string(postgresPoolMax) }
  { name: 'ORQENTIS_EVIDENCE_BLOB_URL', value: evidenceStorage.outputs.blobEndpoint }
  { name: 'ORQENTIS_EVIDENCE_CONTAINER', value: evidenceStorage.outputs.evidenceContainerName }
  { name: 'ORQENTIS_AUDIT_ANCHOR_CONTAINER', value: evidenceStorage.outputs.auditAnchorContainerName }
  // Read-only for the app: only the operators group (auditOperatorsPrincipalId) can write epoch-start markers.
  { name: 'ORQENTIS_AUDIT_EPOCH_CONTAINER', value: evidenceStorage.outputs.auditEpochContainerName }
  // The estate contracts file is a blob in the evidence account (ORQENTIS_EVIDENCE_BLOB_URL): no volume, no secret.
  { name: 'ORQENTIS_CONTRACTS_CONTAINER', value: evidenceStorage.outputs.contractsContainerName }
  { name: 'ORQENTIS_CONTRACTS_BLOB_NAME', value: contractsBlobName }
  { name: 'ORQENTIS_SCHEDULER_AUDIT_ANCHOR_ENABLED', value: auditAnchorEnabled ? 'true' : 'false' }
  { name: 'ORQENTIS_SCHEDULER_AUDIT_ANCHOR_INTERVAL_MINUTES', value: string(auditAnchorIntervalMinutes) }
  { name: 'ORQENTIS_INVESTIGATION_POLL_INTERVAL_SECONDS', value: string(investigationPollIntervalSeconds) }
  { name: 'ORQENTIS_INVESTIGATION_CLAIM_TTL_SECONDS', value: string(investigationClaimTtlSeconds) }
]

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
    { name: 'ORQENTIS_ENABLE_WRITE_ACTIONS', value: enableWriteActions ? 'true' : 'false' }
    { name: 'ORQENTIS_REASONER', value: reasoner }
    { name: 'ORQENTIS_DATA_DIR', value: dataDir }
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
  durableStorageEnv,
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
    : [],
  // Notifications and the web UI. The webhook URLs, the signing key and the CSRF key are secret references.
  [
    { name: 'ORQENTIS_NOTIFIER', value: notifierKind }
    { name: 'ORQENTIS_PUBLIC_BASE_URL', value: publicBaseUrlEffective }
    { name: 'ORQENTIS_UI_ENABLED', value: uiEnabled ? 'true' : 'false' }
  ],
  empty(notifyAllowedHosts) ? [] : [{ name: 'ORQENTIS_NOTIFY_ALLOWED_HOSTS', value: notifyAllowedHosts }],
  uiSignInEnabled ? [{ name: 'ORQENTIS_UI_SIGN_IN_PATH', value: uiSignInPath }] : [],
  map(envVaultReferences, s => { name: s.envName, secretRef: s.secretName })
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

// Same for the notification and web UI inputs (Key Vault URIs, the notifier's secret, the public base URL).
module notificationInputCheck 'modules/input-check.bicep' = {
  name: 'orqentis-check-notification-inputs'
  params: {
    result: any(notificationCheck) // a literal-typed parameter on purpose: ARM, not the compiler, rejects it
  }
}

module readerKeyVaultSecretsUser 'modules/keyvault-role.bicep' = if (!empty(keyVaultResourceId) && !empty(keyVaultSecretNames) && notificationCheck == 'ok') {
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

// ================================================================================================= durable storage

// Fails the deployment with a readable reason when the durable storage or scale inputs cannot work (always
// 'ok' otherwise). The modules below wait for it, so a rejected input never starts creating a database.
module storageInputCheck 'modules/input-check.bicep' = {
  name: 'orqentis-check-durable-storage-inputs'
  params: {
    result: any(storageCheck) // a literal-typed parameter on purpose: ARM, not the compiler, rejects it
  }
}

// PostgreSQL Flexible Server: Entra authentication only. The app identity is created as a database role by the
// bootstrap step (infra/bicep/README.md); nothing about the roles inside the database is deployed from here.
module postgres 'modules/postgres.bicep' = {
  name: 'orqentis-postgres'
  params: {
    location: location
    tags: tags
    serverName: postgresServerName
    existingServerName: postgresExistingServerName
    skuName: postgresSkuName
    storageSizeGiB: postgresStorageSizeGiB
    postgresVersion: postgresVersion
    backupRetentionDays: postgresBackupRetentionDays
    geoRedundantBackup: postgresGeoRedundantBackup
    highAvailabilityMode: postgresHighAvailabilityMode
    entraAdminObjectId: postgresEntraAdminObjectId
    entraAdminName: postgresEntraAdminName
    entraAdminType: postgresEntraAdminType
    tenantId: tenant().tenantId
    databaseName: postgresDatabaseName
    allowedIpRanges: postgresAllowedIpRanges
    allowAzureServices: postgresAllowAzureServices
    delegatedSubnetId: postgresDelegatedSubnetId
    privateDnsZoneId: postgresPrivateDnsZoneId
  }
  dependsOn: [
    storageInputCheck
  ]
}

// Evidence, reports and audit anchors: Blob storage, Entra only, version-level immutability. The reader identity gets
// data access on the two containers only; the actor identity gets none.
module evidenceStorage 'modules/evidence-storage.bicep' = {
  name: 'orqentis-evidence-storage'
  params: {
    location: location
    tags: tags
    accountName: evidenceStorageAccountName
    skuName: storageSkuName
    evidenceContainerName: evidenceContainerName
    auditAnchorContainerName: auditAnchorContainerName
    auditEpochContainerName: auditEpochContainerName
    auditOperatorsPrincipalId: auditOperatorsPrincipalId
    contractsContainerName: contractsContainerName
    retentionDays: evidenceRetentionDays
    immutabilityState: evidenceImmutabilityState
    softDeleteDays: evidenceSoftDeleteDays
    networkDefaultAction: evidenceNetworkDefaultAction
    allowedIpRanges: evidenceAllowedIpRanges
    allowedSubnetIds: evidenceAllowedSubnetIds
    readerPrincipalId: readerIdentity.properties.principalId
  }
  dependsOn: [
    storageInputCheck
  ]
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
          // internal: no public IP for the environment. externalAuthenticated keeps the public endpoint (the app still
          // requires a valid Entra token on every path); the subnet then only carries the environment's outbound traffic.
          internal: effectiveIngressMode != 'externalAuthenticated'
        }
    zoneRedundant: false
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
        keyVaultSecrets,
        map(configuredVaultReferences, s => { name: s.secretName, keyVaultUrl: s.uri, identity: readerIdentity.id })
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
          volumeMounts: useClientCertificate
            ? [
                {
                  volumeName: fabricSpCertificateName
                  mountPath: fabricSpCertificateMountPath
                }
              ]
            : []
        }
      ]
      // The app is stateless, so it can run as several replicas: the database serialises the audit chain (one row-locked
      // chain head), holds the scheduler's leader lease with a fencing generation, makes approvals compare-and-set and
      // keeps the investigation jobs, so replicas never write conflicting state. activeRevisionsMode stays Single: the
      // old and the new revision run side by side during a deploy, which is now safe for the same reasons. See
      // postgresPoolMax for the connection budget (replicas x pool) against the PostgreSQL SKU.
      scale: {
        minReplicas: minReplicas
        maxReplicas: maxReplicas
      }
      volumes: useClientCertificate
        ? [
            {
              // The Key Vault secret (PEM) as a read-only file; the private key never touches a disk.
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
    }
  }
  dependsOn: [
    readerAcrPull
    readerSharedAcrPull
    readerKeyVaultSecretsUser
    servicePrincipalInputCheck
    storageInputCheck
    notificationInputCheck
  ]
}

// Container Apps built-in authentication (Easy Auth) with Microsoft Entra ID. Unauthenticated requests get 401;
// authenticated requests reach the app with X-MS-CLIENT-PRINCIPAL (claims, including app roles). Easy Auth strips any
// client-supplied copy of that header. The app classifies the caller from those claims (app-only tokens are services
// and can never approve), which is why the app registration must emit the idtyp optional claim (README step 1).
// No client secret unless uiSignInClientSecretUri is set: the API only validates bearer tokens. With it, the same
// registration also signs people in for the web UI (authorization code flow at /.auth/login/aad; the cookie session
// carries the ID token, which the app classifies as a person: ADR-0015 row 8). Requests without a token still get 401.
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
    // Browser sessions: no token store (the app needs the ID token's claims only, not an access or refresh token), no
    // redirect to any other site after sign-in, and an 8 hour session.
    login: uiSignInEnabled
      ? {
          preserveUrlFragmentsForLogins: false
          allowedExternalRedirectUrls: []
          tokenStore: {
            enabled: false
          }
          cookieExpiration: {
            convention: 'FixedTime'
            timeToExpiration: '08:00:00'
          }
          nonce: {
            validateNonce: true
          }
        }
      : null
    identityProviders: {
      azureActiveDirectory: {
        enabled: true
        registration: {
          clientId: entraClientId
          openIdIssuer: '${environment().authentication.loginEndpoint}${entraTenantId}/v2.0'
          clientSecretSettingName: uiSignInEnabled ? uiSignInSecretName : null
        }
        validation: {
          allowedAudiences: [
            entraClientId
            'api://${entraClientId}'
          ]
          // With interactive sign-in the registration itself is the client of the browser session, so its own id must be
          // allowed whenever the list is used at all.
          defaultAuthorizationPolicy: empty(allowedClientApplications)
            ? null
            : {
                allowedApplications: uiSignInEnabled
                  ? union(allowedClientApplications, [entraClientId])
                  : allowedClientApplications
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

@description('The origin used for notification deep links and the CSRF check (publicBaseUrl, else derived from the environment).')
output publicBaseUrlEffective string = publicBaseUrlEffective

@description('Redirect URI to register on the Entra app registration (type Web) for interactive sign-in. Empty when uiSignInClientSecretUri is not set.')
output entraRedirectUri string = uiSignInEnabled ? '${publicBaseUrlEffective}/.auth/login/aad/callback' : ''

@description('Where the web UI is: <origin>/ui when uiEnabled.')
output uiUrl string = uiEnabled ? '${publicBaseUrlEffective}/ui' : ''

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

@description('PostgreSQL Flexible Server name (the one the app uses; with postgresExistingServerName, that server).')
output postgresServerName string = postgres.outputs.serverName

@description('PostgreSQL server fully qualified domain name (ORQENTIS_POSTGRES_HOST): used by the bootstrap step.')
output postgresServerFqdn string = postgres.outputs.fqdn

@description('PostgreSQL application database name (ORQENTIS_POSTGRES_DATABASE).')
output postgresDatabaseName string = postgres.outputs.databaseName

@description('Evidence storage account (evidence, reports, audit anchors).')
output evidenceStorageAccountName string = evidenceStorage.outputs.accountName

@description('Blob endpoint of the evidence storage account (ORQENTIS_EVIDENCE_BLOB_URL).')
output evidenceBlobEndpoint string = evidenceStorage.outputs.blobEndpoint

@description('Evidence container name (ORQENTIS_EVIDENCE_CONTAINER).')
output evidenceContainerName string = evidenceStorage.outputs.evidenceContainerName

@description('Audit anchor container name (ORQENTIS_AUDIT_ANCHOR_CONTAINER).')
output auditAnchorContainerName string = evidenceStorage.outputs.auditAnchorContainerName

@description('Audit epoch container name (ORQENTIS_AUDIT_EPOCH_CONTAINER): the audit operators group writes epoch-start markers here after a restore.')
output auditEpochContainerName string = evidenceStorage.outputs.auditEpochContainerName

@description('Estate contracts container name (ORQENTIS_CONTRACTS_CONTAINER): upload the contracts file here (infra/bicep/README.md, Estate contracts).')
output contractsContainerName string = evidenceStorage.outputs.contractsContainerName

@description('Resource id of the alert action group (empty when monitoringEnabled is false).')
output alertActionGroupId string = monitoring.?outputs.actionGroupId ?? ''

@description('Names of the alert rules (empty when monitoringEnabled is false).')
output alertRuleNames array = monitoring.?outputs.alertRuleNames ?? []

@description('Email receivers on the action group; 0 means alerts notify nobody by email.')
output alertEmailReceiverCount int = monitoring.?outputs.emailReceiverCount ?? 0
