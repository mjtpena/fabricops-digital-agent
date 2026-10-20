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
//     EVIDENCE_IMMUTABILITY_LOCKED    'true' or 'false', REQUIRED (no default): whether the evidence immutability policy is
//                          Locked. Locked is IRREVERSIBLE for the retention period and needs the owner's sign-off;
//                          the first deployment must say 'false' (a policy cannot be created Locked). Any other value fails
//                          the build with a message.
//     AUDIT_OPERATORS_OBJECT_ID       optional: object id of the Entra group of operators who may open an audit epoch after a
//                          restore; it gets Storage Blob Data Contributor on the audit-epochs container only
//     CONTAINER_APPS_SUBNET_ID        resource id of the subnet (delegated to Microsoft.App/environments) that VNet-integrates the
//                          Container Apps environment
//     POSTGRES_DELEGATED_SUBNET_ID    resource id of the subnet (delegated to Microsoft.DBforPostgreSQL/flexibleServers) of the
//                          PostgreSQL server
//     POSTGRES_PRIVATE_DNS_ZONE_ID    resource id of the private DNS zone (<name>.private.postgres.database.azure.com) of that server
//   There are no defaults for CONTAINER_IMAGE, ACR_LOGIN_SERVER, ACR_RESOURCE_ID, ENTRA_API_CLIENT_ID,
//   POSTGRES_ENTRA_ADMIN_OBJECT_ID, POSTGRES_ENTRA_ADMIN_NAME, EVIDENCE_IMMUTABILITY_LOCKED, CONTAINER_APPS_SUBNET_ID,
//   POSTGRES_DELEGATED_SUBNET_ID and POSTGRES_PRIVATE_DNS_ZONE_ID: a missing variable fails the deployment instead of
//   deploying a wrong value. The last three describe the private network this environment needs (infra/bicep/README.md,
//   Private networking).
//   `bash scripts/validate-bicep.sh` supplies placeholders so the file can be compiled locally.

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

// Audit epoch markers (README, Storage): the app can only READ the audit-epochs container. Operators who may open an epoch
// after a restore are an Entra group; empty assigns nothing and the owner grants it.
param auditOperatorsPrincipalId = readEnvironmentVariable('AUDIT_OPERATORS_OBJECT_ID', '')

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
// The Container Apps environment is VNet-integrated (CONTAINER_APPS_SUBNET_ID) so that the app reaches PostgreSQL over the
// network and not over the internet. The API itself keeps its public endpoint: every request still needs an Entra token
// (unauthenticated callers get 401), which is what the GitHub-hosted smoke test relies on. For an API that is private as
// well, set ingressMode = 'vnet' (and run the smoke test from a runner inside the network).
param infrastructureSubnetId = readEnvironmentVariable('CONTAINER_APPS_SUBNET_ID')

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

// Evidence storage account: Blob, Entra only, zone-redundant, version-level immutability. Whether the policy is Locked is
// an explicit decision with NO default: the pipeline variable EVIDENCE_IMMUTABILITY_LOCKED must say 'true' or 'false'.
// Locked is IRREVERSIBLE for the retention period (it can then only be extended, and the account cannot be deleted until
// every retention has expired) and needs the owner's sign-off. While it is Unlocked, anyone holding Owner or Storage
// Account Contributor on the account can still delete anchors, audit epoch markers and evidence, so keep those roles
// minimal until it is locked. A policy cannot be created Locked: the first deployment says 'false'.
// 365 days is the proposed retention for the owner to confirm before locking.
param storageSkuName = 'Standard_ZRS'
param evidenceRetentionDays = 365
var evidenceLocked = readEnvironmentVariable('EVIDENCE_IMMUTABILITY_LOCKED')
param evidenceImmutabilityState = evidenceLocked == 'true' ? 'Locked' : (evidenceLocked == 'false' ? 'Unlocked' : 'EVIDENCE_IMMUTABILITY_LOCKED must be true or false')

// PostgreSQL: General Purpose compute, the longest point-in-time restore window. High availability and geo-redundant
// backup are off until the owner decides: ZoneRedundant HA doubles the compute cost, and geo-redundant backup copies
// backups to the paired region (a data-residency decision, fixed when the server is created).
param postgresSkuName = 'Standard_D2ds_v5'
param postgresStorageSizeGiB = 64
param postgresBackupRetentionDays = 35
param postgresHighAvailabilityMode = 'Disabled'
param postgresGeoRedundantBackup = false
// Private networking: the PostgreSQL server has NO public endpoint (VNet integration: a delegated subnet and a private
// DNS zone). The "Allow Azure services" firewall rule, which would open the port to every Azure tenant, is dev-only and
// the template refuses it here; there are no firewall rules at all. Whoever runs the bootstrap or restore steps reaches
// the server from inside the network (infra/bicep/README.md, Private networking). The network itself, and these three
// ids, are provisioned by the owner and come from the environment's GitHub variables.
param postgresDelegatedSubnetId = readEnvironmentVariable('POSTGRES_DELEGATED_SUBNET_ID')
param postgresPrivateDnsZoneId = readEnvironmentVariable('POSTGRES_PRIVATE_DNS_ZONE_ID')

// Alerts (WS9, docs/operations.md): six alert rules and a workbook are deployed with the app, notifying an action group.
// Who is told is NOT in this file: parameter files never hold people or webhooks.
//
//   ALERT_ACTION_GROUP_ID     the resource id of an action group that is managed OUTSIDE this template (created with
//                             `az monitor action-group create`). This is the path for the pipeline: it is an identifier,
//                             so it can be a GitHub variable, whereas a GitHub Actions job prints its environment in the
//                             log of a public repository and would publish addresses. When it is set, every alert uses
//                             that group and the template creates and changes no action group.
//   ALERT_EMAIL_RECEIVERS     comma-separated addresses, for example "oncall@example.com,team@example.com"
//   ALERT_TEAMS_WEBHOOK_URI   a Teams Workflows webhook URL (a secret: keep it in the deployer's secret store)
//                             These two make the template create the action group itself. Use them only where the value
//                             cannot be read by others (a private repository, a local shell), never as GitHub variables
//                             of a public repository. Setting them together with ALERT_ACTION_GROUP_ID fails the deployment.
// All three empty: the rules still evaluate and show in the portal, but nobody is notified.
param alertExistingActionGroupId = readEnvironmentVariable('ALERT_ACTION_GROUP_ID', '')
var alertEmailList = readEnvironmentVariable('ALERT_EMAIL_RECEIVERS', '')
// Whitespace around an address is trimmed ("a@example.com, b@example.com") and empty entries are dropped.
param alertEmailReceivers = filter(map(split(alertEmailList, ','), address => trim(address)), address => !empty(address))
param alertTeamsWebhookUri = readEnvironmentVariable('ALERT_TEAMS_WEBHOOK_URI', '')
