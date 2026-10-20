// Example parameters for infra/bicep/main.bicep: external endpoint, Entra ID token required, optional IP allow list.
// The Container Apps environment is VNet-integrated and PostgreSQL is private: this is what test and prod deploy.
// Placeholders only; contains no secrets and must never contain any.

using '../main.bicep'

param containerImage = 'crfabricops.azurecr.io/orqentis-fabricops:0.1.0'
param entraClientId = '00000000-0000-0000-0000-000000000000'

// PostgreSQL (Microsoft Entra authentication only): the Entra administrator is required. Placeholders; use the object
// id and name of the group whose members run the bootstrap step (infra/bicep/README.md, "PostgreSQL bootstrap").
param postgresEntraAdminObjectId = '00000000-0000-0000-0000-000000000000'
param postgresEntraAdminName = 'sg-fabricops-postgres-admins'

param ingressMode = 'externalAuthenticated'
param infrastructureSubnetId = '/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-network/providers/Microsoft.Network/virtualNetworks/vnet-fabricops/subnets/snet-aca'
param postgresDelegatedSubnetId = '/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-network/providers/Microsoft.Network/virtualNetworks/vnet-fabricops/subnets/snet-postgres'
param postgresPrivateDnsZoneId = '/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-network/providers/Microsoft.Network/privateDnsZones/fabricops.private.postgres.database.azure.com'
// Optional: only these client ranges can connect (everything else is denied before authentication).
// param allowedIpRanges = ['203.0.113.0/24']
