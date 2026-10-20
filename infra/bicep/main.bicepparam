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
