// Grants "Key Vault Secrets User" on specific Key Vault SECRETS (not on the whole vault).
//
// Deployed as a module so the vault can live in a different resource group or subscription from the
// FabricOps resources: main.bicep sets this module's scope to the vault's resource group. The role lets the
// FabricOps reader identity READ the value of exactly the secrets it is given (the Fabric service principal's
// client secret or certificate that Container Apps resolves for it). Scoping the assignment to each secret means
// a compromise of the reader identity cannot read any other secret in the vault; it cannot create, change or
// delete anything either. The vault must use Azure RBAC for data-plane authorization
// (enableRbacAuthorization = true), which is also what makes secret-level role assignments possible.

targetScope = 'resourceGroup'

@description('Name of the existing Key Vault in this resource group.')
param vaultName string

@description('Names of the existing secrets the identity may read. The role is assigned on each secret.')
param secretNames array

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

resource secrets 'Microsoft.KeyVault/vaults/secrets@2023-07-01' existing = [
  for secretName in secretNames: {
    parent: vault
    name: secretName
  }
]

resource secretsUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for (secretName, index) in secretNames: {
    name: guid(secrets[index].id, principalId, keyVaultSecretsUserRoleId)
    scope: secrets[index]
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
]

@description('Resource ids of the role assignments (one per secret).')
output roleAssignmentIds array = [for (secretName, index) in secretNames: secretsUser[index].id]
