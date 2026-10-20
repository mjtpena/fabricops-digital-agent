// Example parameters for infra/bicep/main.bicep: deprecated externalIngress override (kept so existing parameter files behave as before)
// Placeholders only; contains no secrets and must never contain any.

using '../main.bicep'

param containerImage = 'crfabricops.azurecr.io/orqentis-fabricops:0.1.0'
param entraClientId = '00000000-0000-0000-0000-000000000000'
param externalIngress = false   // maps to ingressMode 'internal'
