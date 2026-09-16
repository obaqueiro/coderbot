// Coderbot on Azure: one agent. A Spot VM whose eviction policy is Deallocate, so an
// eviction stops the machine without destroying it: the data disk, OS disk, NIC and
// public IP all survive, and starting it again resumes the task exactly where it
// stopped. A Logic App on a recurrence tries to start it, so capacity coming back is
// enough to bring the agent back with no operator involved.
//
// Deployed into its own resource group by `deploy.sh agent <name>`; the Key Vault,
// VNet and NSG come from the shared deployment.
targetScope = 'resourceGroup'

@description('Identity of this agent (CODEBOT_INSTANCE). Lowercase letters, digits and dashes.')
@minLength(1)
@maxLength(31)
param agentName string

@description('Region. Must be the region of the subnet below.')
param location string = resourceGroup().location

@description('Resource id of the agents subnet from the shared deployment.')
param subnetId string

@description('Name of the shared Key Vault holding the five coderbot secrets.')
param vaultName string

@description('Resource group the shared Key Vault lives in.')
param vaultResourceGroup string

@description('VM size. Defaults to an Ampere arm64 size with 2 vCPU and 8 GB, matching the image this repo builds. Burstable B-series sizes cannot be used: Azure does not offer them as Spot.')
param vmSize string = 'Standard_D2pds_v6'

@description('Marketplace image. The defaults are Ubuntu 24.04 LTS for arm64; for an x86 size use sku "server".')
param imagePublisher string = 'Canonical'
param imageOffer string = 'ubuntu-24_04-lts'
param imageSku string = 'server-arm64'

@description('OS disk size in GB. Only the OS lives here; all coderbot state is on the data disk.')
param osDiskSizeGb int = 30
param osDiskType string = 'StandardSSD_LRS'

@description('Data disk size in GB. Holds coderbot state, both checkouts and the Docker data-root. Azure bills managed disks by tier, so 64 and 128 are the sizes that do not waste money.')
param dataDiskSizeGb int = 64

@description('Data disk SKU. StandardSSD_LRS is the cheap default; Premium_LRS roughly doubles the price for much better IOPS during image builds and end-to-end runs.')
@allowed([
  'StandardSSD_LRS'
  'Premium_LRS'
  'StandardSSD_ZRS'
  'Premium_ZRS'
])
param dataDiskType string = 'StandardSSD_LRS'

@description('Optional snapshot resource id to restore the data disk from, for moving an agent to another region or recovering one.')
param dataDiskSnapshotId string = ''

@description('Admin user on the VM. Coderbot itself runs in a container as uid 501, not as this user.')
param adminUsername string = 'coderbot'

@description('SSH public key for the admin user. Azure requires one on every Linux VM even when no inbound rule allows SSH.')
param adminSshKey string

@description('Base64 cloud-init payload rendered by deploy.sh (writes /etc/coderbot/agent.conf and runs the bootstrap).')
param customData string

@description('Maximum Spot price per hour as a string, or "-1" to pay up to the pay-as-you-go price and be evicted only when capacity runs out.')
param maxSpotPrice string = '-1'

@description('Start the VM again automatically after an eviction.')
param enableAutoStart bool = true

@description('How often the auto-start Logic App retries, in minutes.')
@minValue(5)
@maxValue(1440)
param autoStartMinutes int = 15

// Virtual Machine Contributor, narrowed to this one VM by the assignment scope below.
var vmContributorRoleId = '9980e02c-c2be-4d73-94e8-173b1dc7cf3c'

var tags = {
  'coderbot:agent': agentName
}

// ---------------------------------------------------------------- state
// The disk is its own resource with deleteOption Detach on the VM, so recreating or
// resizing the VM never touches the agent state. This is what `deploy.sh destroy`
// snapshots before it deletes anything.
resource dataDisk 'Microsoft.Compute/disks@2024-03-02' = {
  name: 'coderbot-${agentName}-data'
  location: location
  tags: tags
  sku: {
    name: dataDiskType
  }
  properties: {
    diskSizeGB: dataDiskSizeGb
    creationData: empty(dataDiskSnapshotId) ? {
      createOption: 'Empty'
    } : {
      createOption: 'Copy'
      sourceResourceId: dataDiskSnapshotId
    }
  }
}

// ---------------------------------------------------------------- network
// Outbound only. Azure retired implicit outbound access for new virtual networks, so
// the VM needs an address of its own; a Standard public IP is the cheapest way to get
// one, and the shared NSG keeps inbound closed.
resource publicIp 'Microsoft.Network/publicIPAddresses@2024-05-01' = {
  name: 'coderbot-${agentName}-ip'
  location: location
  tags: tags
  sku: {
    name: 'Standard'
  }
  properties: {
    publicIPAllocationMethod: 'Static'
  }
}

resource nic 'Microsoft.Network/networkInterfaces@2024-05-01' = {
  name: 'coderbot-${agentName}-nic'
  location: location
  tags: tags
  properties: {
    ipConfigurations: [
      {
        name: 'ipconfig'
        properties: {
          subnet: {
            id: subnetId
          }
          publicIPAddress: {
            id: publicIp.id
          }
          privateIPAllocationMethod: 'Dynamic'
        }
      }
    ]
  }
}

// ---------------------------------------------------------------- the agent
resource vm 'Microsoft.Compute/virtualMachines@2024-07-01' = {
  name: 'coderbot-${agentName}'
  location: location
  tags: tags
  identity: {
    // System-assigned so the VM can read the Key Vault secrets through IMDS without
    // deploy.sh having to know a client id before the VM exists.
    type: 'SystemAssigned'
  }
  properties: {
    hardwareProfile: {
      vmSize: vmSize
    }
    priority: 'Spot'
    // Deallocate, not Delete: an eviction stops this VM and leaves every disk in place.
    evictionPolicy: 'Deallocate'
    billingProfile: {
      maxPrice: json(maxSpotPrice)
    }
    storageProfile: {
      imageReference: {
        publisher: imagePublisher
        offer: imageOffer
        sku: imageSku
        version: 'latest'
      }
      osDisk: {
        createOption: 'FromImage'
        diskSizeGB: osDiskSizeGb
        managedDisk: {
          storageAccountType: osDiskType
        }
        deleteOption: 'Delete'
      }
      dataDisks: [
        {
          lun: 0
          createOption: 'Attach'
          caching: 'None'
          deleteOption: 'Detach'
          managedDisk: {
            id: dataDisk.id
          }
        }
      ]
    }
    osProfile: {
      computerName: 'coderbot-${agentName}'
      adminUsername: adminUsername
      customData: customData
      linuxConfiguration: {
        disablePasswordAuthentication: true
        ssh: {
          publicKeys: [
            {
              path: '/home/${adminUsername}/.ssh/authorized_keys'
              keyData: adminSshKey
            }
          ]
        }
      }
    }
    networkProfile: {
      networkInterfaces: [
        {
          id: nic.id
        }
      ]
    }
    diagnosticsProfile: {
      // Managed boot diagnostics: the serial log and a screenshot when a boot fails.
      bootDiagnostics: {
        enabled: true
      }
    }
  }
}

// Let the VM read the five secrets, and nothing else in the vault.
module vaultAccess 'modules/vault-role.bicep' = {
  name: 'coderbot-${agentName}-vault-role'
  scope: resourceGroup(vaultResourceGroup)
  params: {
    vaultName: vaultName
    principalId: vm.identity.principalId
  }
}

// ---------------------------------------------------------------- auto-start
// Starting a VM that is already running is a no-op, so the workflow needs no condition:
// it simply asks for a start every few minutes, and the request succeeds as soon as
// Spot capacity is available again.
resource autoStart 'Microsoft.Logic/workflows@2019-05-01' = if (enableAutoStart) {
  name: 'coderbot-${agentName}-autostart'
  location: location
  tags: tags
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    state: 'Enabled'
    definition: {
      '$schema': 'https://schema.management.azure.com/providers/Microsoft.Logic/schemas/2016-06-01/workflowdefinition.json#'
      contentVersion: '1.0.0.0'
      parameters: {}
      triggers: {
        Recurrence: {
          type: 'Recurrence'
          recurrence: {
            frequency: 'Minute'
            interval: autoStartMinutes
          }
        }
      }
      actions: {
        StartVm: {
          type: 'Http'
          inputs: {
            method: 'POST'
            uri: '${environment().resourceManager}subscriptions/${subscription().subscriptionId}/resourceGroups/${resourceGroup().name}/providers/Microsoft.Compute/virtualMachines/${vm.name}/start?api-version=2024-07-01'
            authentication: {
              type: 'ManagedServiceIdentity'
              audience: environment().resourceManager
            }
          }
        }
      }
      outputs: {}
    }
  }
}

resource autoStartRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (enableAutoStart) {
  name: guid(vm.id, 'autostart', vmContributorRoleId)
  scope: vm
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', vmContributorRoleId)
    // Non-null assertion: this assignment shares the enableAutoStart condition, so the
    // workflow always exists whenever this resource does.
    principalId: autoStart!.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

output vmName string = vm.name
output publicIp string = publicIp.properties.ipAddress
output dataDiskId string = dataDisk.id
output principalId string = vm.identity.principalId
