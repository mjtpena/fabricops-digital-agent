// Example: FabricOps runs in one Azure tenant and monitors a Fabric tenant that belongs to another Entra tenant.
//
//   az deployment group create -g rg-fabricops-prod \
//     -f infra/bicep/main.bicep -p infra/bicep/examples/cross-tenant.bicepparam
//
// A managed identity exists only in the tenant that hosts it, so it cannot be given Fabric access across
// tenants. Instead the app signs in as a service principal that is registered in (or consented in) the FABRIC
// tenant. Its credential is a Key Vault reference resolved by Container Apps with the reader managed identity;
// no secret value is ever a parameter, an output or a file in this repository. See infra/bicep/README.md,
// "Cross-tenant Fabric access". Contains placeholders only.

using '../main.bicep'

param prefix = 'fabricops'
param containerImage = 'crfabricops.azurecr.io/orqentis-fabricops:0.1.0'
param containerRegistryName = 'crfabricops'
param entraClientId = '00000000-0000-0000-0000-000000000000'

param deploymentEnvironment = 'prod'
param fabricMode = 'rest'
param contractsPath = '/var/lib/orqentis/contracts/estate.json'

// The Fabric estate's tenant (ORQENTIS_TENANT_ID and AZURE_TENANT_ID). NOT this deployment's tenant.
param fabricTenantId = '11111111-1111-1111-1111-111111111111'
param defaultTimezone = 'Europe/London'

param fabricAuthMode = 'service_principal'
// Application (client) id of the service principal in the Fabric tenant. Not a secret.
param fabricServicePrincipalClientId = '22222222-2222-2222-2222-222222222222'

// Option 1: client secret held in Key Vault (versionless URI, so a rotated secret is picked up on restart).
param fabricServicePrincipalSecretUri = 'https://kv-fabricops.vault.azure.net/secrets/fabric-sp-secret'
// Option 2: certificate held in Key Vault as PEM (set this INSTEAD of the secret URI, not as well).
// param fabricServicePrincipalSecretUri = ''
// param fabricServicePrincipalCertificateUri = 'https://kv-fabricops.vault.azure.net/secrets/fabric-sp-cert'

// Optional: let the template grant the reader identity "Key Vault Secrets User" on the referenced secret(s) only,
// not on the whole vault (Azure RBAC vaults).
param keyVaultResourceId = '/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-shared/providers/Microsoft.KeyVault/vaults/kv-fabricops'
