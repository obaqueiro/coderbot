// Coderbot on Azure: the pieces every agent shares. Deploy once per subscription and
// region with `deploy.sh shared`; each agent then gets its own resource group from
// agent.bicep. Nothing here is agent-specific, so adding an agent never touches it.
targetScope = 'resourceGroup'

@description('Region for the shared resources. Agents default to this region too.')
param location string = resourceGroup().location

@description('Address space of the VNet the agents run in.')
param vnetCidr string = '10.42.0.0/16'

@description('Address space of the agents subnet.')
param subnetCidr string = '10.42.1.0/24'

@description('Optional CIDR allowed to reach the agents on SSH, e.g. "203.0.113.4/32". Empty leaves the VMs with no inbound rule at all (the default: operate them with `deploy.sh exec/logs`, which go through the Azure control plane).')
param allowSshFrom string = ''

// Key Vault names are globally unique, 3-24 chars, alphanumerics and dashes. Derive one
// from the resource group id so a redeploy is stable and two subscriptions never clash.
var vaultName = 'kv${uniqueString(resourceGroup().id)}'

var sshRules = empty(allowSshFrom) ? [] : [
  {
    name: 'allow-ssh'
    properties: {
      priority: 300
      direction: 'Inbound'
      access: 'Allow'
      protocol: 'Tcp'
      sourceAddressPrefix: allowSshFrom
      sourcePortRange: '*'
      destinationAddressPrefix: '*'
      destinationPortRange: '22'
    }
  }
]

// Azure's default rules already deny all inbound from the internet, so an empty rule
// list is a closed VM. Outbound is left open (GitHub, Anthropic, Google, registries).
resource nsg 'Microsoft.Network/networkSecurityGroups@2024-05-01' = {
  name: 'coderbot-agents'
  location: location
  properties: {
    securityRules: sshRules
  }
}

resource vnet 'Microsoft.Network/virtualNetworks@2024-05-01' = {
  name: 'coderbot'
  location: location
  properties: {
    addressSpace: {
      addressPrefixes: [vnetCidr]
    }
    subnets: [
      {
        name: 'agents'
        properties: {
          addressPrefix: subnetCidr
          networkSecurityGroup: {
            id: nsg.id
          }
        }
      }
    ]
  }
}

// The five files every agent needs. Values are pushed by `deploy.sh secrets` with the
// Azure CLI, never through this template, so they stay out of deployment history.
// RBAC (not access policies) so an agent VM's managed identity can be granted
// "Key Vault Secrets User" on the vault and nothing more.
resource vault 'Microsoft.KeyVault/vaults@2024-11-01' = {
  name: vaultName
  location: location
  properties: {
    tenantId: subscription().tenantId
    sku: {
      family: 'A'
      name: 'standard'
    }
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 7
    // Purge protection is deliberately left off: it cannot be undone, and it would keep
    // a deleted vault (and its name) alive for the full retention period.
    publicNetworkAccess: 'Enabled'
  }
}

output vaultName string = vault.name
output vaultUri string = vault.properties.vaultUri
output subnetId string = vnet.properties.subnets[0].id
output location string = location
output resourceGroupName string = resourceGroup().name
