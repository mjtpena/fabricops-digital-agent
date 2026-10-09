// Grants "Cognitive Services OpenAI User" on one Azure OpenAI (Cognitive Services) account.
//
// Deployed as a module so the account can live in a different resource group or subscription from the
// FabricOps resources: main.bicep sets this module's scope to the account's resource group.
// The role allows inference calls (chat completions) only; it cannot read keys, deploy models or change
// the account.

targetScope = 'resourceGroup'

@description('Name of the existing Azure OpenAI / Azure AI Services account (Microsoft.CognitiveServices/accounts) in this resource group.')
param accountName string

@description('Object (principal) id of the identity that receives the role: the FabricOps reader identity.')
param principalId string

@description('Principal type of principalId. User-assigned managed identities are service principals.')
@allowed([
  'ServicePrincipal'
  'Group'
  'User'
])
param principalType string = 'ServicePrincipal'

// Built-in role: Cognitive Services OpenAI User.
var cognitiveServicesOpenAiUserRoleId = '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'

resource account 'Microsoft.CognitiveServices/accounts@2023-05-01' existing = {
  name: accountName
}

resource openAiUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(account.id, principalId, cognitiveServicesOpenAiUserRoleId)
  scope: account
  properties: {
    description: 'Orqentis FabricOps reader identity: Azure OpenAI inference for the agent_framework reasoner.'
    principalId: principalId
    principalType: principalType
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      cognitiveServicesOpenAiUserRoleId
    )
  }
}

@description('Resource id of the role assignment.')
output roleAssignmentId string = openAiUser.id
