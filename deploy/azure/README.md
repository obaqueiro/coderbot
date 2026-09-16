# Coderbot on Azure Spot VMs

Runs any number of coderbot agents in your own Azure subscription, each on a Spot VM
with a persistent managed disk, so an eviction costs you a few minutes rather than the
task. Bicep and the Azure CLI only: nothing here depends on this repository's CI or on
a particular subscription. The AWS equivalent lives in [`../aws`](../aws/README.md).

```
subscription (once)              per agent, resource group "coderbot-<name>"
┌──────────────────────┐         ┌────────────────────────────────────────────┐
│ shared.bicep         │  read   │ agent.bicep                                │
│  Key Vault, 5 secrets│◄────────│  managed disk (snapshotted on destroy)     │
│  VNet + subnet       │         │  Spot VM, eviction policy Deallocate       │
│  NSG (no inbound)    │         │  public IP + NIC                           │
│                      │         │  Logic App that starts the VM again        │
└──────────────────────┘         └────────────────────────────────────────────┘
```

## How an eviction is survived

Azure evicts a Spot VM by **deallocating** it, not deleting it. The data disk, OS disk,
network interface and public IP all stay exactly as they were, so recovery is a boot
rather than a rebuild. There is no disk to re-attach and no repository to re-clone.

1. The watcher on the VM polls Azure Scheduled Events every two seconds. A Spot
   eviction shows up there as a `Preempt` event about thirty seconds ahead.
2. It stops the container. A SIGTERM makes the agent drop its in-flight tick and exit
   within seconds, then the watcher acknowledges the event so Azure proceeds at once.
3. The VM deallocates. Billing for it stops; the disks keep costing their few dollars.
4. The Logic App asks Azure to start the VM every fifteen minutes. Starting a running
   VM is a no-op, so this needs no condition and no state. The first request after
   capacity returns brings the agent back.
5. On boot, systemd reseeds nothing that already exists, then starts the container. It
   comes up with the same `instance_id`, re-asserts its own backlog claim, finds
   `state.json` at the phase that was interrupted, and re-runs that phase.

Only the single agent invocation that was running is lost, which is the same recovery
as a crash. Unlike the AWS deployment, the agent is pinned to a **region** but not to an
availability zone, so Spot capacity anywhere in the region can take it.

Everything durable lives on the data disk, mounted at `/datadrive` (Azure keeps the VM's
ephemeral resource disk at `/mnt`, so the persistent one cannot go there):

| Path | Holds |
|---|---|
| `data/` | `state.json`, `instance_id`, `holds.json`, `processed_msgs.json`, `token.json`, transcripts, outbox |
| `claude/` | the bot's `~/.claude`: `.credentials.json` and the session files `claude --resume` reads |
| `repo/<target>` | the target repository working copy, including uncommitted agent work |
| `coderbot/` | this repository at the pinned ref, which is the build context |
| `docker/` | Docker's data-root: image layers, build cache, the end-to-end stack's images |
| `codebot.env`, `agent.env`, `claude.json` | the shared `.env`, the per-agent overrides, the `~/.claude.json` seed |

## Prerequisites

- The Azure CLI, signed in, with `az account set` pointed at the subscription you want.
- Permission to create resource groups, role assignments and Key Vault secrets.
- An SSH public key. Azure requires one on every Linux VM even though no inbound rule
  allows SSH by default.
- The files coderbot needs, produced once on your machine by `scripts/setup.sh`:
  `.env` (with `GH_TOKEN` and `CLAUDE_CODE_OAUTH_TOKEN`), `data/token.json`,
  `data/credentials.json`, `~/.claude/.credentials.json`, `~/.claude.json`.
- `CODEBOT_INSTANCE` and `CODEBOT_REPO_PATH` in that `.env` are ignored. Each agent gets
  its own from its deployment.

## Deploy

```bash
cd deploy/azure
./deploy.sh shared --location eastus
./deploy.sh secrets --env ../../.env --data ../../data
./deploy.sh agent nccr --target-repo getriverly/riverly
./deploy.sh agent nccr2 --target-repo getriverly/riverly
./deploy.sh status                       # every agent: size, power state, disk, container
./deploy.sh logs nccr                    # the container log, over the Azure control plane
```

`agent` options: `--location`, `--size` (default `Standard_D2pds_v6`), `--ref`
(branch, tag or commit of coderbot), `--disk` and `--disk-type`, `--ssh-key`,
`--max-price` (`-1` pays up to the pay-as-you-go price and is evicted only when capacity
runs out), `--auto-start-minutes`, `--no-auto-start`, `--snapshot` (restore a disk),
`--reseed` (push the current secrets onto an existing disk on next boot; they are
otherwise seeded once, because the agent refreshes tokens in place), `--coderbot-url`
(your fork), `--image-sku` (use `server` for an x86 size).

Day two:

| Command | Effect |
|---|---|
| `deploy.sh update NAME` | fetch the pinned ref again, rebuild the image, restart the container |
| `deploy.sh agent NAME ... --size X` | change parameters; Bicep applies the change in place |
| `deploy.sh restart NAME` | restart the container only |
| `deploy.sh stop NAME` | stop the agent cleanly, then deallocate the VM so it stops billing |
| `deploy.sh start NAME` | start it again |
| `deploy.sh exec NAME -- df -h /datadrive` | run any command on the VM, no inbound port needed |
| `deploy.sh destroy NAME` | snapshot the data disk into the shared group, then delete the agent |
| `deploy.sh prices --location eastus` | live Spot and pay-as-you-go prices for the candidate sizes |

`stop` is the honest way to pause an agent: a deallocated VM costs nothing, and the disk
keeps the task exactly where it was. To retire an agent that is mid-task, send it
`ABORT <name>` by email first, as the main README describes, so it unclaims the task.

## Cost per agent

Retail list prices in East US, read from the public Azure retail prices API on
2026-09-15. Run `deploy.sh prices` for current numbers in your own region; Spot prices
move continuously and vary a lot between regions.

| Item | Per hour | Per month |
|---|---|---|
| `Standard_D2pds_v6` Spot, 2 vCPU and 8 GB | $0.0331 | ~$24 |
| the same size at pay-as-you-go, for comparison | $0.0918 | ~$67 |
| `Standard_D2ps_v5` Spot, the cheaper older Ampere | $0.0163 | ~$12 |
| 64 GB Standard SSD data disk | | $4.80 |
| 30 GB Standard SSD OS disk | | $2.40 |
| Standard static public IP | | ~$3.65 |
| the auto-start Logic App at a 15-minute recurrence | | ~$0.15 |

So roughly $35 a month per agent on the newer Ampere size, or about $23 on
`Standard_D2ps_v5`, against $77 for the same machine at pay-as-you-go. A deallocated
agent costs only its disks and IP, about $11 a month.

## Caveats

- **Thirty seconds of warning.** Azure gives far less notice than the two minutes on
  AWS, which is why the watcher polls every two seconds and the container stop timeout
  is short. The agent's SIGTERM handler is what makes this fit.
- **Region pinning.** The disk cannot leave its region. To move an agent, `destroy` it
  (which snapshots the disk), then deploy it again with `--snapshot`.
- **A quiet subscription quota still bites.** If the region has no Spot capacity for the
  size, the Logic App keeps asking and the agent stays down until capacity returns.
  Compare sizes with `deploy.sh prices` and pick a less contended one.
- **Shared credentials.** All agents use one `.env`, one Gmail account, one GitHub token
  and one Claude login. Identity is the `CODEBOT_INSTANCE` override per deployment.
- **Key Vault secrets hold at most 25 KB each**, which all five of these files are
  comfortably under.
- **The image is built on the VM.** An agent's first boot takes several minutes; later
  boots reuse the build cache on the data disk. There is no registry.
- The container runs as uid 501, as the repository Dockerfile sets, and `boot.sh` chowns
  the disk to match.

## Files

- `shared.bicep`: Key Vault, VNet, subnet and NSG. Deployed once per region.
- `agent.bicep`: one agent. Disk, public IP, NIC, Spot VM, the auto-start Logic App and
  the role assignments for both.
- `modules/vault-role.bicep`: grants a VM's managed identity read access to the secrets.
  A module because the vault is in a different resource group than the agent.
- `deploy.sh`: the CLI. Bicep deployments plus a few VM, disk and run-command calls.
- `instance/bootstrap.sh`: the cloud-init payload. Installs Docker, mounts the data
  disk, clones this repository, then hands over. It rides inside custom data as gzip and
  base64, so it needs no template escaping.
- `instance/boot.sh`: runs on every boot as `coderbot-boot.service` and on `update`.
  Reads the secrets from Key Vault through the VM's managed identity, clones the target
  repository, writes `agent.env`, refreshes the systemd units.
- `instance/coderbot.service`, `instance/coderbot-boot.service`,
  `instance/coderbot-spot-watch.service`, `instance/spot-watch.sh`: the container unit,
  its prerequisite, and the Scheduled Events watcher.
- `docker-compose.azure.yml`: the compose file with every path on the data disk.
