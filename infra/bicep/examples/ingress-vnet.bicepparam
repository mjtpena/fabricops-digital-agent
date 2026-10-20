// Example parameters for infra/bicep/main.bicep: VNet-only (internal environment, no public IP)
// Placeholders only; contains no secrets and must never contain any.

using '../main.bicep'

param containerImage = 'crfabricops.azurecr.io/orqentis-fabricops:0.1.0'
param entraClientId = '00000000-0000-0000-0000-000000000000'

// PostgreSQL (Microsoft Entra authentication only): the Entra administrator is required. Placeholders; use the object
// id and name of the group whose members run the bootstrap step (infra/bicep/README.md, "PostgreSQL bootstrap").
param postgresEntraAdminObjectId = '00000000-0000-0000-0000-000000000000'
param postgresEntraAdminName = 'sg-fabricops-postgres-admins'

param ingressMode = 'vnet'
param infrastructureSubnetId = '/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-network/providers/Microsoft.Network/virtualNetworks/vnet-fabricops/subnets/snet-aca'

// With a VNet, PostgreSQL can be VNet-integrated as well (no public endpoint, no firewall rules): a subnet delegated to
// Microsoft.DBforPostgreSQL/flexibleServers (a different subnet from the one above) and a private DNS zone linked to
// the VNet. Reachable only from the VNet, so run the bootstrap step from a host inside it.
// param postgresDelegatedSubnetId = '/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-network/providers/Microsoft.Network/virtualNetworks/vnet-fabricops/subnets/snet-postgres'
// param postgresPrivateDnsZoneId = '/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-network/providers/Microsoft.Network/privateDnsZones/fabricops.private.postgres.database.azure.com'
