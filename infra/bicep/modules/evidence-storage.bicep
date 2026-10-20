// Storage account for evidence, reports and audit anchors (ADR-0008): Blob storage, Microsoft Entra only, with
// version-level immutability (WORM) so what is written cannot be changed or deleted before its retention ends.
//
//   * Shared Key access is OFF and so is SAS by account key: there are no account keys in use anywhere. People and
//     the app sign in with Entra (az ... --auth-mode login).
//   * Version-level immutability is enabled for the account (all containers), with a default retention policy of
//     retentionDays that every new blob version inherits. state Unlocked can still be removed by someone with
//     management rights on the account (the app has none); Locked cannot be shortened or removed, ever, and blocks
//     deleting the account until every retention has run out. Locking is a deliberate production step.
//   * Versioning, blob soft delete and container soft delete are on, so an overwrite or delete leaves a recoverable
//     version for softDeleteDays.
//   * Four containers: evidence and reports, the periodic audit-chain anchors, the audit epoch-start markers, and the
//     estate contracts file. Who may write where (always at container scope, never a role on the account):
//       - the reader identity (the app): Storage Blob Data Contributor on evidence and audit-anchors; only Storage Blob
//         Data Reader on audit-epochs and contracts, so a compromised app cannot open an audit epoch or rewrite the
//         declared estate;
//       - the audit operators group (auditOperatorsPrincipalId, optional): Storage Blob Data Contributor on audit-epochs
//         ONLY, to write the marker `orqentis audit start-epoch` needs after a restore. Nothing is assigned when the
//         parameter is empty;
//       - whoever uploads the estate contracts: nothing from this template. The owner assigns Storage Blob Data
//         Contributor on the contracts container to the people or identities that do (infra/bicep/README.md);
//       - the actor (WRITE) identity: nothing.
//     Nobody but the deploy identity has management-plane rights on the account (and so on its immutability policy).
//   * hierarchical namespace is off: the Blob API is what the Azurite emulator speaks, and the same client adapter can
//     target a OneLake blob URL instead (ORQENTIS_EVIDENCE_BLOB_URL).

targetScope = 'resourceGroup'

@description('Azure region.')
param location string

@description('Tags applied to the account.')
param tags object

@description('Storage account name: 3-24 lowercase letters and digits, globally unique.')
@minLength(3)
@maxLength(24)
param accountName string

@description('Storage redundancy.')
@allowed([
  'Standard_LRS'
  'Standard_ZRS'
])
param skuName string = 'Standard_LRS'

@description('Name of the container for evidence and incident reports.')
@minLength(3)
@maxLength(63)
param evidenceContainerName string = 'evidence'

@description('Name of the container for audit-chain anchors.')
@minLength(3)
@maxLength(63)
param auditAnchorContainerName string = 'audit-anchors'

@description('Name of the container for the audit epoch-start markers. The app reads it; the audit operators group writes to it.')
@minLength(3)
@maxLength(63)
param auditEpochContainerName string = 'audit-epochs'

@description('Optional: object id of the Entra group of operators who may open an audit epoch after a restore. Gets Storage Blob Data Contributor on the audit-epochs container only. Empty: no role is assigned.')
param auditOperatorsPrincipalId string = ''

@description('Name of the container for the estate contracts file. The app reads it; people upload to it.')
@minLength(3)
@maxLength(63)
param contractsContainerName string = 'contracts'

@description('Default retention (days since creation) of every blob version.')
@minValue(1)
@maxValue(3650)
param retentionDays int = 30

@description('Unlocked: the policy can still be changed or removed by an account owner (testing, and production until the owner decides). Locked: irreversible; the retention can only be extended.')
@allowed([
  'Unlocked'
  'Locked'
])
param immutabilityState string = 'Unlocked'

@description('Days a deleted or overwritten blob (and a deleted container) stays recoverable.')
@minValue(1)
@maxValue(365)
param softDeleteDays int = 14

@description('Network default action. Allow: the public endpoint answers any caller that has an Entra token with a role. Deny: only the IP ranges and subnets below (and trusted Azure services) can connect.')
@allowed([
  'Allow'
  'Deny'
])
param networkDefaultAction string = 'Allow'

@description('Used with networkDefaultAction Deny: IPv4 addresses or CIDR ranges (a single address without a mask; /31 and /32 are not accepted) allowed to connect.')
param allowedIpRanges array = []

@description('Used with networkDefaultAction Deny: resource ids of subnets (with the Microsoft.Storage service endpoint) allowed to connect.')
param allowedSubnetIds array = []

@description('Object (principal) id of the reader identity, the only identity that receives data access.')
param readerPrincipalId string

// Built-in role: Storage Blob Data Contributor (read, write and delete blobs; it cannot change policies or keys).
var storageBlobDataContributorRoleId = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
// Built-in role: Storage Blob Data Reader (read and list blobs; it cannot write or delete).
var storageBlobDataReaderRoleId = '2a2b9908-6ea1-4ae2-8e65-a410df84e7d1'

resource account 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: accountName
  location: location
  tags: tags
  sku: {
    name: skuName
  }
  kind: 'StorageV2'
  properties: {
    accessTier: 'Hot'
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    allowBlobPublicAccess: false
    allowCrossTenantReplication: false
    defaultToOAuthAuthentication: true
    // No account keys in use: not for the app, not for the pipeline, not for the portal.
    allowSharedKeyAccess: false
    isHnsEnabled: false
    publicNetworkAccess: 'Enabled'
    networkAcls: {
      defaultAction: networkDefaultAction
      bypass: 'AzureServices'
      ipRules: [
        for range in allowedIpRanges: {
          value: range
          action: 'Allow'
        }
      ]
      virtualNetworkRules: [
        for subnetId in allowedSubnetIds: {
          id: subnetId
          action: 'Allow'
        }
      ]
    }
    // Version-level immutability for every container of the account. It can only be switched on when the account is
    // created, and it needs (and turns on) blob versioning.
    immutableStorageWithVersioning: {
      enabled: true
      immutabilityPolicy: {
        immutabilityPeriodSinceCreationInDays: retentionDays
        state: immutabilityState
        allowProtectedAppendWrites: false
      }
    }
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: account
  name: 'default'
  properties: {
    isVersioningEnabled: true
    deleteRetentionPolicy: {
      enabled: true
      days: softDeleteDays
    }
    containerDeleteRetentionPolicy: {
      enabled: true
      days: softDeleteDays
    }
    changeFeed: {
      enabled: false
    }
  }
}

resource evidenceContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: evidenceContainerName
  properties: {
    publicAccess: 'None'
    immutableStorageWithVersioning: {
      enabled: true
    }
  }
}

resource auditAnchorContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: auditAnchorContainerName
  properties: {
    publicAccess: 'None'
    immutableStorageWithVersioning: {
      enabled: true
    }
  }
}

resource auditEpochContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: auditEpochContainerName
  properties: {
    publicAccess: 'None'
    immutableStorageWithVersioning: {
      enabled: true
    }
  }
}

resource contractsContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: contractsContainerName
  properties: {
    publicAccess: 'None'
    immutableStorageWithVersioning: {
      enabled: true
    }
  }
}

// Scoped to each container, not to the account: the app can read and write the evidence and anchor containers and
// nothing else in this account (it cannot list or create containers, read account settings or touch policies).
resource readerEvidenceAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(evidenceContainer.id, readerPrincipalId, storageBlobDataContributorRoleId)
  scope: evidenceContainer
  properties: {
    description: 'Orqentis FabricOps reader identity: read and write evidence and reports (this container only).'
    principalId: readerPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageBlobDataContributorRoleId)
  }
}

resource readerAuditAnchorAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(auditAnchorContainer.id, readerPrincipalId, storageBlobDataContributorRoleId)
  scope: auditAnchorContainer
  properties: {
    description: 'Orqentis FabricOps reader identity: write and read audit-chain anchors (this container only).'
    principalId: readerPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageBlobDataContributorRoleId)
  }
}

// The audit epoch container is READ-ONLY for the app: an epoch-start marker records that the audit history was cut
// by a restore, and it must be something a compromised app identity cannot forge or remove. The app reads the markers;
// only the operators group below can write them.
resource readerAuditEpochAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(auditEpochContainer.id, readerPrincipalId, storageBlobDataReaderRoleId)
  scope: auditEpochContainer
  properties: {
    description: 'Orqentis FabricOps reader identity: read the audit epoch-start markers (this container only, read-only).'
    principalId: readerPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageBlobDataReaderRoleId)
  }
}

// The people who may open an audit epoch after a restore: Contributor on this one container, only when a group is given.
// Nothing is assigned by default; the owner decides who is an audit operator.
resource operatorsAuditEpochAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(auditOperatorsPrincipalId)) {
  name: guid(auditEpochContainer.id, auditOperatorsPrincipalId, storageBlobDataContributorRoleId)
  scope: auditEpochContainer
  properties: {
    description: 'Orqentis FabricOps audit operators: write audit epoch-start markers after a restore (this container only).'
    principalId: auditOperatorsPrincipalId
    principalType: 'Group'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageBlobDataContributorRoleId)
  }
}

// The contracts container is READ-ONLY for the app: a compromised reader identity cannot rewrite the declared estate
// (topology, SLAs). Uploading is a human or pipeline task that the owner grants separately; this template assigns
// nothing to an uploader.
resource readerContractsAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(contractsContainer.id, readerPrincipalId, storageBlobDataReaderRoleId)
  scope: contractsContainer
  properties: {
    description: 'Orqentis FabricOps reader identity: read the estate contracts file (this container only, read-only).'
    principalId: readerPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageBlobDataReaderRoleId)
  }
}

@description('Storage account name.')
output accountName string = account.name

@description('Blob endpoint of the account (ORQENTIS_EVIDENCE_BLOB_URL), without a trailing slash.')
output blobEndpoint string = 'https://${account.name}.blob.${environment().suffixes.storage}'

@description('Evidence container name (ORQENTIS_EVIDENCE_CONTAINER).')
output evidenceContainerName string = evidenceContainer.name

@description('Audit anchor container name (ORQENTIS_AUDIT_ANCHOR_CONTAINER).')
output auditAnchorContainerName string = auditAnchorContainer.name

@description('Audit epoch container name (ORQENTIS_AUDIT_EPOCH_CONTAINER).')
output auditEpochContainerName string = auditEpochContainer.name

@description('Estate contracts container name (ORQENTIS_CONTRACTS_CONTAINER).')
output contractsContainerName string = contractsContainer.name
