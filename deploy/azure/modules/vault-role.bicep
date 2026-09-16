// Grants one principal read access to the shared Key Vault's secrets. Lives in a module
// because the vault sits in the shared resource group while the agent is deployed into
// its own, and a role assignment must be deployed at the scope it applies to.
targetScope = 'resourceGroup'

@description('Name of the Key Vault in this resource group.')
param vaultName string

@description('Object id of the identity to grant access to (the agent VM managed identity).')
param principalId string

// Key Vault Secrets User: read secret values, nothing else.
var secretsUserRoleId = '4633458b-17de-408a-b874-0445c86b69e6'

resource vault 'Microsoft.KeyVault/vaults@2024-11-01' existing = {
  name: vaultName
}

resource assignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(vault.id, principalId, secretsUserRoleId)
  scope: vault
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', secretsUserRoleId)
    principalId: principalId
    principalType: 'ServicePrincipal'
  }
}
