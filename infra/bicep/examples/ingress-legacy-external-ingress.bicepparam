// Example parameters for infra/bicep/main.bicep: deprecated externalIngress override (kept so existing parameter files behave as before)
// Placeholders only; contains no secrets and must never contain any.

using '../main.bicep'

param containerImage = 'crfabricops.azurecr.io/orqentis-fabricops:0.1.0'
param entraClientId = '00000000-0000-0000-0000-000000000000'

// PostgreSQL (Microsoft Entra authentication only): the Entra administrator is required. Placeholders; use the object
// id and name of the group whose members run the bootstrap step (infra/bicep/README.md, "PostgreSQL bootstrap").
param postgresEntraAdminObjectId = '00000000-0000-0000-0000-000000000000'
param postgresEntraAdminName = 'sg-fabricops-postgres-admins'

// No VNet in this example, so it is a dev deployment: outside dev PostgreSQL must be private (see
// ingress-external-authenticated.bicepparam), and the Container Apps outbound addresses are not fixed, so the
// "Allow Azure services" firewall rule is what lets the app reach PostgreSQL. It opens the port to every Azure tenant.
param deploymentEnvironment = 'dev'
param postgresAllowAzureServices = true

param externalIngress = false   // maps to ingressMode 'internal'
