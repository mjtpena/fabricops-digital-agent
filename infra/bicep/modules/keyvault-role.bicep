// Grants "Key Vault Secrets User" on one Key Vault.
//
// Deployed as a module so the vault can live in a different resource group or subscription from the
// FabricOps resources: main.bicep sets this module's scope to the vault's resource group. The role lets the
// FabricOps reader identity READ secret values (the Fabric service principal's client secret or certificate
// that Container Apps resolves for it); it cannot create, change or delete secrets, keys or certificates.
// The vault must use Azure RBAC for data-plane authorization (enableRbacAuthorization = true).

targetScope = 'resourceGroup'

@description('Name of the existing Key Vault in this resource group.')
param vaultName string

@description('Object (principal) id of the identity that receives the role: the FabricOps reader identity.')
param principalId string

@description('Principal type of principalId. User-assigned managed identities are service principals.')
@allowed([
  'ServicePrincipal'
  'Group'
  'User'
])
param principalType string = 'ServicePrincipal'

// Built-in role: Key Vault Secrets User.
var keyVaultSecretsUserRoleId = '4633458b-17de-408a-b874-0445c86b69e6'

resource vault 'Microsoft.KeyVault/vaults@2023-07-01' existing = {
  name: vaultName
}

resource secretsUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(vault.id, principalId, keyVaultSecretsUserRoleId)
  scope: vault
  properties: {
    description: 'Orqentis FabricOps reader identity: read the Fabric service principal credential held in Key Vault.'
    principalId: principalId
    principalType: principalType
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      keyVaultSecretsUserRoleId
    )
  }
}

@description('Resource id of the role assignment.')
output roleAssignmentId string = secretsUser.id
