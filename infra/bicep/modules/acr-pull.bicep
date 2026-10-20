// Grants "AcrPull" on one Azure Container Registry.
//
// Deployed as a module so the registry can live in a different resource group or subscription from the
// FabricOps resources, such as the shared registry rg-fabricops-shared/crfabricopsshared that every
// environment pulls from: main.bicep sets this module's scope to the registry's resource group.
// AcrPull can pull images only; it cannot push, delete or change the registry.

targetScope = 'resourceGroup'

@description('Name of the existing Azure Container Registry (Microsoft.ContainerRegistry/registries) in this resource group.')
param registryName string

@description('Object (principal) id of the identity that receives the role: the FabricOps reader identity.')
param principalId string

@description('Principal type of principalId. User-assigned managed identities are service principals.')
@allowed([
  'ServicePrincipal'
  'Group'
  'User'
])
param principalType string = 'ServicePrincipal'

// Built-in role: AcrPull.
var acrPullRoleId = '7f951dda-4ed3-4680-a7ca-43fe172d538d'

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: registryName
}

resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, principalId, acrPullRoleId)
  scope: registry
  properties: {
    description: 'Orqentis FabricOps reader identity: pull the app image from the shared registry.'
    principalId: principalId
    principalType: principalType
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
  }
}

@description('Resource id of the role assignment.')
output roleAssignmentId string = acrPull.id
