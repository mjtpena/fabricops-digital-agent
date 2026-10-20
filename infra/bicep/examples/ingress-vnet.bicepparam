// Example parameters for infra/bicep/main.bicep: VNet-only (internal environment, no public IP)
// Placeholders only; contains no secrets and must never contain any.

using '../main.bicep'

param containerImage = 'crfabricops.azurecr.io/orqentis-fabricops:0.1.0'
param entraClientId = '00000000-0000-0000-0000-000000000000'
param ingressMode = 'vnet'
param infrastructureSubnetId = '/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-network/providers/Microsoft.Network/virtualNetworks/vnet-fabricops/subnets/snet-aca'
