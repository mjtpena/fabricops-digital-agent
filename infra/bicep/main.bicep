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
//     which only that front end can set.
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

// ------------------------------------------------------------------------------------------------- network + auth

@description('false (default): ingress is reachable only from inside the Container Apps environment. true: reachable from outside the environment (from the internet, or only from the VNet when infrastructureSubnetId is set). Every request is still authenticated by Microsoft Entra ID.')
param externalIngress bool = false

@description('Optional: resource id of a subnet (delegated to Microsoft.App/environments, /27 or larger) for an internal, VNet-integrated environment with no public endpoint. Empty: no VNet integration.')
param infrastructureSubnetId string = ''

@description('Application (client) id of the Microsoft Entra app registration that represents the FabricOps API (defines the FabricOps.Operator and FabricOps.PlatformAdmin app roles).')
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

@description('Optional: full resource id of the Key Vault named in the URIs above. When set, the reader identity is granted "Key Vault Secrets User" on it (the vault must use Azure RBAC; it may be in another resource group or subscription). Leave empty if access is granted some other way.')
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

// The secrets (by name) the reader identity must read; the Key Vault role is assigned on these, not on the vault.
// Names are the segment after /secrets/ in https://<vault>.vault.azure.net/secrets/<name>[/<version>].
var keyVaultSecretNames = concat(
  useClientSecret ? [split(fabricServicePrincipalSecretUri, '/')[4]] : [],
  useClientCertificate ? [split(fabricServicePrincipalCertificateUri, '/')[4]] : []
)

// service_principal inputs checked when the deployment runs (see modules/input-check.bicep): 'ok', or what is wrong.
// Nested conditionals are evaluated lazily, so a parse of a URI only happens after the URI is known to be set.
var credentialUri = empty(fabricServicePrincipalSecretUri)
  ? fabricServicePrincipalCertificateUri
  : fabricServicePrincipalSecretUri
var servicePrincipalCheck = !servicePrincipalMode
  ? 'ok'
  : empty(fabricServicePrincipalClientId)
      ? 'fabricAuthMode is service_principal, so fabricServicePrincipalClientId (the application id of the service principal in the Fabric tenant) must be set'
      : length(fabricTenantId) != 36
          ? 'fabricAuthMode is service_principal, so fabricTenantId must be set to the id (a GUID) of the Fabric tenant, not left empty'
          : empty(fabricServicePrincipalSecretUri) == empty(fabricServicePrincipalCertificateUri)
              ? 'fabricAuthMode is service_principal, so set exactly one of fabricServicePrincipalSecretUri and fabricServicePrincipalCertificateUri'
              : !startsWith(credentialUri, 'https://') || !contains(credentialUri, '/secrets/') || length(split(credentialUri, '/')) < 5
                  ? 'the Key Vault credential URI must look like https://<vault-name>.<key-vault-dns-suffix>/secrets/<secret-name> (the secrets endpoint, not keys or certificates)'
                  : !empty(keyVaultResourceId) && !startsWith(split(credentialUri, '/')[2], '${keyVaultIdSegments[8]}.')
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
    { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', secretRef: appInsightsSecretName }
  ],
  empty(azureOpenAiEndpoint) ? [] : [{ name: 'ORQENTIS_AZURE_OPENAI_ENDPOINT', value: azureOpenAiEndpoint }],
  empty(azureOpenAiDeployment) ? [] : [{ name: 'ORQENTIS_AZURE_OPENAI_DEPLOYMENT', value: azureOpenAiDeployment }],
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
    vnetConfiguration: empty(infrastructureSubnetId)
      ? null
      : {
          infrastructureSubnetId: infrastructureSubnetId
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
        external: externalIngress
        targetPort: appPort
        transport: 'http'
        allowInsecure: false
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
          probes: [
            {
              // Startup can include a full read of the Fabric estate (rest mode); give it time before liveness.
              type: 'Startup'
              httpGet: {
                path: '/health'
                port: appPort
                scheme: 'HTTP'
              }
              initialDelaySeconds: 5
              periodSeconds: 15
              timeoutSeconds: 5
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
                path: '/health'
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
      // SQLite (event store, state DB) and the hash-chained audit log allow a single writer only. Do not raise
      // maxReplicas until these stores move to Eventhouse / OneLake.
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
    readerKeyVaultSecretsUser
    servicePrincipalInputCheck
  ]
}

// Container Apps built-in authentication (Easy Auth) with Microsoft Entra ID. Unauthenticated requests get 401;
// authenticated requests reach the app with X-MS-CLIENT-PRINCIPAL (claims, including app roles).
// No client secret: the API only validates bearer tokens, it never runs an interactive login flow.
// Authentication is required on every path because unauthenticatedClientAction is not AllowAnonymous and no
// excludedPaths are set (Container Apps has no separate "requireAuthentication" flag, unlike App Service).
// Health probes go straight to the container port and do not pass through this front end.
resource authConfig 'Microsoft.App/containerApps/authConfigs@2024-03-01' = {
  parent: containerApp
  name: 'current'
  properties: {
    platform: {
      enabled: true
    }
    globalValidation: {
      unauthenticatedClientAction: 'Return401'
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
        }
      }
    }
  }
}

// ================================================================================================= outputs

@description('Name of the Container App.')
output containerAppName string = containerApp.name

@description('Ingress FQDN of the Container App (environment-internal unless externalIngress is true).')
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
