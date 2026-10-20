// Example parameters for infra/bicep/main.bicep: external endpoint, Entra ID token required, optional IP allow list
// Placeholders only; contains no secrets and must never contain any.

using '../main.bicep'

param containerImage = 'crfabricops.azurecr.io/orqentis-fabricops:0.1.0'
param entraClientId = '00000000-0000-0000-0000-000000000000'

// PostgreSQL (Microsoft Entra authentication only): the Entra administrator is required. Placeholders; use the object
// id and name of the group whose members run the bootstrap step (infra/bicep/README.md, "PostgreSQL bootstrap").
param postgresEntraAdminObjectId = '00000000-0000-0000-0000-000000000000'
param postgresEntraAdminName = 'sg-fabricops-postgres-admins'

param ingressMode = 'externalAuthenticated'
// Optional: only these client ranges can connect (everything else is denied before authentication).
// param allowedIpRanges = ['203.0.113.0/24']
