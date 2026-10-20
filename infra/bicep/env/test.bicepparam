// Parameters for the TEST environment (resource group rg-fabricops-test). Used by .github/workflows/cd.yml and
// whatif.yml; see docs/delivery.md. Contains no secrets and must never contain any.
//
//   Values supplied by the pipeline (GitHub environment/repository variables, all identifiers, none secret):
//     CONTAINER_IMAGE      <registry>/orqentis-fabricops@sha256:<digest>   the image promoted by digest
//     ACR_LOGIN_SERVER     login server of the shared registry
//     ACR_RESOURCE_ID      full resource id of the shared registry (the template grants the reader identity AcrPull)
//     ENTRA_API_CLIENT_ID  client id of the "Orqentis FabricOps API" app registration for this environment
//     FABRIC_MODE          optional override of fabricMode ('mock' or 'rest')
//   There are no defaults for the first four: a missing variable fails the deployment instead of deploying a wrong
//   value. `bash scripts/validate-bicep.sh` supplies placeholders so the file can be compiled locally.

using '../main.bicep'

param prefix = 'fabricops-test'

param tags = {
  application: 'orqentis-fabricops'
  environment: 'test'
  purpose: 'test'
  owner: 'fabricops-programme'
}

param containerImage = readEnvironmentVariable('CONTAINER_IMAGE')
param containerRegistryServer = readEnvironmentVariable('ACR_LOGIN_SERVER')
param containerRegistryResourceId = readEnvironmentVariable('ACR_RESOURCE_ID')

param entraClientId = readEnvironmentVariable('ENTRA_API_CLIENT_ID')
// param entraTenantId = '<tenant id>'   // defaults to the deployment tenant

param deploymentEnvironment = 'test'
// 'mock' serves the synthetic estate baked into the image, so test needs no Fabric access or contracts file.
param fabricMode = readEnvironmentVariable('FABRIC_MODE', 'mock')
param contractsPath = '/var/lib/orqentis/contracts/estate.json'

// The WRITE capability boundary stays closed in every environment until the executor ships (WS11).
param enableWriteActions = false

// Deterministic reasoner. To use the Agent Framework reasoner set all three Azure OpenAI values.
param reasoner = 'rule_based'
param azureOpenAiEndpoint = ''
param azureOpenAiDeployment = ''
param azureOpenAiResourceId = ''

// Reachable from the GitHub runner for the smoke test; every request still needs an Entra token (unauthenticated
// callers get 401). Ingress modes are owned by WS3; keep this in step with the mode it introduces.
param externalIngress = true
param infrastructureSubnetId = ''

param containerCpu = '0.25'
param containerMemory = '0.5Gi'
param logRetentionInDays = 30
param storageSkuName = 'Standard_LRS'
param fileShareQuotaGiB = 8
