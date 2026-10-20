// Azure Database for PostgreSQL Flexible Server: the Orqentis FabricOps system of record (ADR-0008).
//
// Incidents, approvals, events, scheduler leases and the audit chain live here. The server is Microsoft Entra
// authentication ONLY (passwordAuth Disabled): there is no administrator login and no password anywhere, so the app
// connects with its managed identity and people connect with their own Entra token. The Entra administrator is a
// group (or user, or service principal) given as parameters; the app identity is created as a database role by the
// bootstrap step in infra/bicep/README.md ("PostgreSQL bootstrap").
//
// Network: public access with firewall rules (default none) or, when a delegated subnet and a private DNS zone are
// given, VNet integration with no public endpoint. Firewall rules and VNet integration are mutually exclusive.
//
// With existingServerName set the module creates NOTHING: it only reads the FQDN of that server, so the app can be
// pointed at a server restored from backup (docs/operations/restore.md) without this template touching it.

targetScope = 'resourceGroup'

@description('Azure region.')
param location string

@description('Tags applied to the server.')
param tags object

@description('Name of the server to create (lowercase letters, digits, hyphens; 3-63 characters).')
@minLength(3)
@maxLength(63)
param serverName string

@description('Name of an EXISTING server in this resource group to use instead of creating one (for example a point-in-time restore). Empty: create serverName. Nothing about an existing server is changed.')
param existingServerName string = ''

@description('Compute SKU. Standard_B* is Burstable, Standard_D* General Purpose, Standard_E* Memory Optimized; the tier is derived from the name.')
param skuName string = 'Standard_B1ms'

@description('Storage size in GiB. Storage auto-grows; a deployment must never ask for less than the current size.')
@allowed([
  32
  64
  128
  256
  512
  1024
  2048
  4096
  8192
  16384
  32767
])
param storageSizeGiB int = 32

@description('Major PostgreSQL version. Only used when the server is created.')
@allowed([
  '14'
  '15'
  '16'
  '17'
])
param postgresVersion string = '17'

@description('Days of backups kept for point-in-time restore (7-35).')
@minValue(7)
@maxValue(35)
param backupRetentionDays int = 7

@description('Geo-redundant backup: copy backups to the paired region. Can only be chosen when the server is created.')
param geoRedundantBackup bool = false

@description('High availability: Disabled, ZoneRedundant (standby in another zone) or SameZone. Not available on the Burstable tier.')
@allowed([
  'Disabled'
  'ZoneRedundant'
  'SameZone'
])
param highAvailabilityMode string = 'Disabled'

@description('Object id of the Microsoft Entra principal that administers the server (a group is recommended).')
param entraAdminObjectId string

@description('Display name of that principal; it is the PostgreSQL role name people sign in with (for a group, the group name).')
param entraAdminName string

@description('Principal type of the Entra administrator.')
@allowed([
  'Group'
  'User'
  'ServicePrincipal'
])
param entraAdminType string = 'Group'

@description('Microsoft Entra tenant that issues the tokens the server accepts.')
param tenantId string

@description('Name of the application database to create.')
@minLength(1)
@maxLength(63)
param databaseName string = 'orqentis'

@description('Public access only: IP ranges allowed through the server firewall. Must be empty with VNet integration.')
param allowedIpRanges firewallRule[] = []

@description('Public access only: add the 0.0.0.0-0.0.0.0 rule that lets Azure services (Container Apps without a static outbound address) reach the TCP port. Every connection still needs an Entra token.')
param allowAzureServices bool = false

@description('VNet integration: resource id of a subnet delegated to Microsoft.DBforPostgreSQL/flexibleServers. Requires privateDnsZoneId; there is then no public endpoint.')
param delegatedSubnetId string = ''

@description('VNet integration: resource id of the private DNS zone (<name>.private.postgres.database.azure.com) linked to the VNet.')
param privateDnsZoneId string = ''

type firewallRule = {
  @description('Rule name: letters, digits, hyphens and underscores.')
  @minLength(1)
  @maxLength(128)
  name: string

  @description('First IPv4 address of the range.')
  startIpAddress: string

  @description('Last IPv4 address of the range (equal to the first for a single address).')
  endIpAddress: string
}

var adopt = !empty(existingServerName)
var vnetIntegrated = !empty(delegatedSubnetId)

// Burstable (B-series), Memory Optimized (E-series), otherwise General Purpose (D-series).
var tier = startsWith(skuName, 'Standard_B')
  ? 'Burstable'
  : (startsWith(skuName, 'Standard_E') ? 'MemoryOptimized' : 'GeneralPurpose')

var azureServicesRule = allowAzureServices
  ? [
      {
        name: 'allow-azure-services'
        startIpAddress: '0.0.0.0'
        endIpAddress: '0.0.0.0'
      }
    ]
  : []
var effectiveFirewallRules = (adopt || vnetIntegrated) ? [] : concat(azureServicesRule, allowedIpRanges)

resource server 'Microsoft.DBforPostgreSQL/flexibleServers@2025-08-01' = if (!adopt) {
  name: serverName
  location: location
  tags: tags
  sku: {
    name: skuName
    tier: tier
  }
  properties: {
    version: postgresVersion
    createMode: 'Default'
    storage: {
      storageSizeGB: storageSizeGiB
      autoGrow: 'Enabled'
    }
    backup: {
      backupRetentionDays: backupRetentionDays
      geoRedundantBackup: geoRedundantBackup ? 'Enabled' : 'Disabled'
    }
    highAvailability: {
      mode: highAvailabilityMode
    }
    // Entra only: no administrator login, no password. Sign-in needs an Entra access token for the
    // https://ossrdbms-aad.database.windows.net resource.
    authConfig: {
      activeDirectoryAuth: 'Enabled'
      passwordAuth: 'Disabled'
      tenantId: tenantId
    }
    // VNet integration has no public endpoint, and the platform rejects publicNetworkAccess for it; otherwise the
    // public endpoint is on and the firewall rules below decide who can open the port.
    network: vnetIntegrated
      ? {
          delegatedSubnetResourceId: delegatedSubnetId
          privateDnsZoneArmResourceId: privateDnsZoneId
        }
      : {
          publicNetworkAccess: 'Enabled'
        }
  }
}

// The children of a flexible server cannot be changed in parallel (the server answers "busy"), so they are chained:
// administrator, database, server parameter, firewall rules (one at a time).
resource entraAdmin 'Microsoft.DBforPostgreSQL/flexibleServers/administrators@2025-08-01' = if (!adopt) {
  parent: server
  name: entraAdminObjectId
  properties: {
    principalType: entraAdminType
    principalName: entraAdminName
    tenantId: tenantId
  }
}

resource database 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2025-08-01' = if (!adopt) {
  parent: server
  name: databaseName
  properties: {
    charset: 'UTF8'
    collation: 'en_US.utf8'
  }
  dependsOn: [
    entraAdmin
  ]
}

// TLS is required for every connection (this is also the platform default; stating it keeps it from drifting).
resource requireSecureTransport 'Microsoft.DBforPostgreSQL/flexibleServers/configurations@2025-08-01' = if (!adopt) {
  parent: server
  name: 'require_secure_transport'
  properties: {
    value: 'ON'
    source: 'user-override'
  }
  dependsOn: [
    database
  ]
}

@batchSize(1)
resource firewall 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2025-08-01' = [
  for rule in effectiveFirewallRules: {
    parent: server
    name: rule.name
    properties: {
      startIpAddress: rule.startIpAddress
      endIpAddress: rule.endIpAddress
    }
    dependsOn: [
      requireSecureTransport
    ]
  }
]

// ARM may evaluate names of condition-false resources; keep the name well-formed when nothing is adopted.
resource existingServer 'Microsoft.DBforPostgreSQL/flexibleServers@2025-08-01' existing = if (adopt) {
  name: empty(existingServerName) ? 'unused' : existingServerName
}

@description('Name of the server the app uses.')
output serverName string = adopt ? existingServerName : server.name

@description('Fully qualified domain name of the server (ORQENTIS_POSTGRES_HOST).')
output fqdn string = adopt
  ? existingServer!.properties.fullyQualifiedDomainName
  : server!.properties.fullyQualifiedDomainName

@description('Name of the application database (ORQENTIS_POSTGRES_DATABASE).')
output databaseName string = databaseName
