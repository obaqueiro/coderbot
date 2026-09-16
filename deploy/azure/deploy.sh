#!/usr/bin/env bash
# Coderbot on Azure: one Spot VM and one persistent managed disk per agent, driven by
# Bicep and the Azure CLI only. Point `az account set` at any subscription.
#
#   deploy.sh shared  [--location eastus] [--allow-ssh-from CIDR|myip]   once per region
#   deploy.sh secrets [--env .env] [--claude-dir ~/.claude] [--claude-json ~/.claude.json] [--data data]
#   deploy.sh agent   NAME --target-repo owner/name [--location L] [--size Standard_D2pds_v6]
#                     [--ref main] [--disk 64] [--disk-type StandardSSD_LRS] [--ssh-key PATH]
#                     [--max-price -1] [--auto-start-minutes 15] [--no-auto-start]
#                     [--snapshot ID] [--reseed] [--coderbot-url URL]
#   deploy.sh status  [NAME]        power state, size, Spot price cap, disk, container
#   deploy.sh logs    NAME [-n 200] the container log, over the Azure control plane
#   deploy.sh exec    NAME -- CMD   run a command on the VM (no inbound port needed)
#   deploy.sh update  NAME          fetch the pinned ref again, rebuild, restart
#   deploy.sh restart NAME          restart the container in place
#   deploy.sh stop    NAME          stop the agent and deallocate the VM (billing stops)
#   deploy.sh start   NAME          start it again
#   deploy.sh destroy NAME [--no-snapshot]   snapshot the data disk, delete the agent
#   deploy.sh prices  [--location L] [--size S]   live Spot vs pay-as-you-go prices
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SHARED_RG="${CODERBOT_SHARED_RG:-coderbot-shared}"
SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/coderbot-azure.XXXXXX")"; trap 'rm -rf "$SCRATCH"' EXIT

die() { echo "error: $*" >&2; exit 1; }
need_tools() {
  command -v az >/dev/null || die "the Azure CLI (az) is required: https://learn.microsoft.com/cli/azure/install-azure-cli"
  command -v python3 >/dev/null || die "python3 is required"
}

agent_rg() { echo "coderbot-$1"; }
vm_name()  { echo "coderbot-$1"; }
require_name() { [[ "${1:-}" =~ ^[a-z0-9][a-z0-9-]{0,30}$ ]] || die "agent NAME must match [a-z0-9][a-z0-9-]{0,30}"; }

shared_output() {  # shared_output KEY
  az deployment group show -g "$SHARED_RG" -n coderbot-shared \
    --query "properties.outputs.$1.value" -o tsv 2>/dev/null \
    || die "shared deployment not found in resource group $SHARED_RG; run: deploy.sh shared"
}

# ------------------------------------------------------------------ shared
cmd_shared() {
  local location="" allow_ssh=""
  while [ $# -gt 0 ]; do case "$1" in
    --location) location="$2"; shift 2 ;;
    --allow-ssh-from) allow_ssh="$2"; shift 2 ;;
    *) die "unknown option $1" ;;
  esac; done
  [ -n "$location" ] || location=$(az config get defaults.location --query value -o tsv 2>/dev/null || true)
  [ -n "$location" ] || die "--location is required (for example: --location eastus)"
  if [ "$allow_ssh" = myip ]; then
    # Asks a public echo service for this machine's own address, nothing else.
    allow_ssh="$(curl -fsS https://checkip.amazonaws.com | tr -d '[:space:]')/32"
    echo "allowing SSH from $allow_ssh"
  fi
  az group create -n "$SHARED_RG" -l "$location" -o none
  az deployment group create -g "$SHARED_RG" -n coderbot-shared -f "$HERE/shared.bicep" \
    -p location="$location" allowSshFrom="$allow_ssh" -o none
  echo "shared resources ready in $SHARED_RG ($location)"
  echo "  key vault: $(shared_output vaultName)"
  echo "next: deploy.sh secrets, then deploy.sh agent NAME --target-repo owner/name"
}

# ------------------------------------------------------------------ secrets
cmd_secrets() {
  local env=".env" claude_dir="$HOME/.claude" claude_json="$HOME/.claude.json" data="data"
  while [ $# -gt 0 ]; do case "$1" in
    --env) env="$2"; shift 2 ;; --claude-dir) claude_dir="$2"; shift 2 ;;
    --claude-json) claude_json="$2"; shift 2 ;; --data) data="$2"; shift 2 ;;
    *) die "unknown option $1" ;;
  esac; done
  local vault; vault=$(shared_output vaultName)
  [ -s "$env" ] || die "$env not found (pass --env)"
  grep -q '^GH_TOKEN=' "$env" || die "$env has no GH_TOKEN"
  put() {  # put SECRET FILE
    if [ -s "$2" ]; then
      # A Key Vault secret holds at most 25 KB; these files are all far smaller.
      az keyvault secret set --vault-name "$vault" --name "$1" --file "$2" -o none
      echo "  $1  <- $2"
    else
      echo "  $1  skipped ($2 missing or empty)"
    fi
  }
  echo "pushing secrets into $vault:"
  put env "$env"
  put claude-credentials "$claude_dir/.credentials.json"
  put claude-json "$claude_json"
  put google-token "$data/token.json"
  put google-credentials "$data/credentials.json"
  echo "note: an agent seeds these files once; use 'deploy.sh agent NAME ... --reseed' to push new values onto an existing disk"
}

# ------------------------------------------------------------------ agent
render_cloud_init() {  # render_cloud_init NAME TARGET_REPO URL REF RESEED VAULT_URI
  cat <<CLOUDINIT
#!/bin/bash
mkdir -p /etc/coderbot
cat > /etc/coderbot/agent.conf <<'EOC'
AGENT_NAME=$1
TARGET_REPO=$2
CODERBOT_REPO_URL=$3
CODERBOT_REF=$4
RESEED_SECRETS=$5
VAULT_URI=$6
EOC
base64 -d <<'EOB' | gunzip > /usr/local/sbin/coderbot-bootstrap
$(gzip -9c "$HERE/instance/bootstrap.sh" | base64)
EOB
chmod 0755 /usr/local/sbin/coderbot-bootstrap
exec /usr/local/sbin/coderbot-bootstrap
CLOUDINIT
}

default_ssh_key() {
  local k
  for k in "$HOME/.ssh/id_ed25519.pub" "$HOME/.ssh/id_rsa.pub"; do
    [ -s "$k" ] && { echo "$k"; return; }
  done
  die "no SSH public key found; pass --ssh-key PATH or run: ssh-keygen -t ed25519"
}

cmd_agent() {
  local name="${1:-}"; shift || true; require_name "$name"
  local target="" location="" size="Standard_D2pds_v6" ref="main" disk=64 disk_type="StandardSSD_LRS"
  local ssh_key="" max_price="-1" auto_start=true auto_minutes=15 snapshot="" reseed=false
  local url="https://github.com/obaqueiro/coderbot.git" image_sku="server-arm64"
  while [ $# -gt 0 ]; do case "$1" in
    --target-repo) target="$2"; shift 2 ;; --location) location="$2"; shift 2 ;;
    --size) size="$2"; shift 2 ;; --ref) ref="$2"; shift 2 ;;
    --disk) disk="$2"; shift 2 ;; --disk-type) disk_type="$2"; shift 2 ;;
    --ssh-key) ssh_key="$2"; shift 2 ;; --max-price) max_price="$2"; shift 2 ;;
    --auto-start-minutes) auto_minutes="$2"; shift 2 ;; --no-auto-start) auto_start=false; shift ;;
    --snapshot) snapshot="$2"; shift 2 ;; --reseed) reseed=true; shift ;;
    --coderbot-url) url="$2"; shift 2 ;; --image-sku) image_sku="$2"; shift 2 ;;
    *) die "unknown option $1" ;;
  esac; done
  [ -n "$target" ] || die "--target-repo owner/name is required"
  [[ "$target" == */* ]] || die "--target-repo must be owner/name"
  [ -n "$ssh_key" ] || ssh_key=$(default_ssh_key)
  [ -s "$ssh_key" ] || die "SSH public key $ssh_key not found"
  local vault subnet; vault=$(shared_output vaultName); subnet=$(shared_output subnetId)
  [ -n "$location" ] || location=$(shared_output location)
  if [[ "$size" == Standard_B* ]]; then
    echo "warning: burstable B-series Spot is barely discounted and its CPU credits throttle long agent runs; compare with: deploy.sh prices --location $location" >&2
  fi

  render_cloud_init "$name" "$target" "$url" "$ref" "$reseed" "https://$vault.vault.azure.net" > "$SCRATCH/cloud-init"
  python3 - "$SCRATCH/cloud-init" "$SCRATCH/params.json" <<PY
import base64, json, sys
custom = base64.b64encode(open(sys.argv[1], 'rb').read()).decode()
params = {
    "agentName": "$name", "location": "$location", "subnetId": "$subnet",
    "vaultName": "$vault", "vaultResourceGroup": "$SHARED_RG",
    "vmSize": "$size", "imageSku": "$image_sku",
    "dataDiskSizeGb": int("$disk"), "dataDiskType": "$disk_type",
    "dataDiskSnapshotId": "$snapshot", "adminSshKey": open("$ssh_key").read().strip(),
    "customData": custom, "maxSpotPrice": "$max_price",
    "enableAutoStart": "$auto_start" == "true", "autoStartMinutes": int("$auto_minutes"),
}
json.dump({"\$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
           "contentVersion": "1.0.0.0",
           "parameters": {k: {"value": v} for k, v in params.items()}},
          open(sys.argv[2], "w"))
PY
  az group create -n "$(agent_rg "$name")" -l "$location" --tags "coderbot:agent=$name" -o none
  echo "deploying agent $name in $location ($size, Spot), target $target, ref $ref"
  az deployment group create -g "$(agent_rg "$name")" -n "coderbot-agent-$name" \
    -f "$HERE/agent.bicep" -p "@$SCRATCH/params.json" -o none
  echo "deployed. The first boot builds the image, which takes several minutes."
  echo "follow with: deploy.sh status $name   /   deploy.sh logs $name"
}

# ------------------------------------------------------------------ operations
run_on() {  # run_on NAME SCRIPT... -> prints the command output
  local name="$1"; shift
  az vm run-command invoke -g "$(agent_rg "$name")" -n "$(vm_name "$name")" \
    --command-id RunShellScript --scripts "$@" --query 'value[0].message' -o tsv
}

power_state() {
  az vm get-instance-view -g "$(agent_rg "$1")" -n "$(vm_name "$1")" \
    --query "instanceView.statuses[?starts_with(code, 'PowerState')].displayStatus | [0]" -o tsv 2>/dev/null || true
}

cmd_status() {
  local names=("$@")
  if [ ${#names[@]} -eq 0 ]; then
    mapfile -t names < <(az group list --query "[?tags.\"coderbot:agent\"!=null].tags.\"coderbot:agent\"" -o tsv 2>/dev/null || true)
  fi
  [ ${#names[@]} -gt 0 ] || { echo "no agents (resource groups tagged coderbot:agent)"; return; }
  for name in "${names[@]}"; do
    local rg; rg=$(agent_rg "$name")
    echo "== $name  (resource group $rg)"
    az vm show -g "$rg" -n "$(vm_name "$name")" \
      --query '[hardwareProfile.vmSize, priority, evictionPolicy, to_string(billingProfile.maxPrice), location]' -o tsv 2>/dev/null \
      | awk -F'\t' '{printf "   size=%s priority=%s eviction=%s maxPrice=%s region=%s\n", $1,$2,$3,$4,$5}' \
      || { echo "   not deployed"; continue; }
    local state; state=$(power_state "$name")
    echo "   power: ${state:-unknown}"
    az disk show -g "$rg" -n "coderbot-$name-data" --query '[diskSizeGb, sku.name, diskState]' -o tsv 2>/dev/null \
      | awk -F'\t' '{printf "   data disk: %s GB %s (%s)\n", $1,$2,$3}'
    if [ "$state" = "VM running" ]; then
      run_on "$name" 'docker ps --filter name=codebot --format "   container={{.Names}} {{.Status}}"; systemctl is-active coderbot.service | sed "s/^/   unit: /"' 2>/dev/null \
        | sed -n '/^ *container=\|^ *unit:/p' || echo "   (the VM is not answering run-command yet)"
    else
      echo "   container: not running (the VM is $state)"
    fi
  done
}

cmd_logs() {
  require_name "${1:-}"; local name="$1"; shift
  local n=200
  while [ $# -gt 0 ]; do case "$1" in -n|--lines) n="$2"; shift 2 ;; *) die "unknown option $1" ;; esac; done
  [ "$(power_state "$name")" = "VM running" ] || die "$name is not running (power state: $(power_state "$name"))"
  run_on "$name" "cd /datadrive/coderbot && docker compose --env-file /datadrive/agent.env -f deploy/azure/docker-compose.azure.yml logs --tail $n --no-color"
}

cmd_exec() {
  require_name "${1:-}"; local name="$1"; shift
  [ "${1:-}" = "--" ] && shift
  [ $# -gt 0 ] || die "give a command, for example: deploy.sh exec $name -- df -h /datadrive"
  run_on "$name" "$*"
}

cmd_update() {
  require_name "${1:-}"; local name="$1"
  echo "updating $name: fetch the pinned ref, rebuild the image, restart the container"
  run_on "$name" '. /etc/coderbot/agent.conf
git -C /datadrive/coderbot fetch --tags origin
git -C /datadrive/coderbot checkout -q --detach "origin/$CODERBOT_REF" 2>/dev/null || git -C /datadrive/coderbot checkout -q --detach "$CODERBOT_REF"
git -C /datadrive/coderbot log -1 --oneline
systemctl restart coderbot-boot.service
systemctl restart coderbot.service
systemctl is-active coderbot.service'
}

cmd_restart() { require_name "${1:-}"; run_on "$1" 'systemctl restart coderbot.service; systemctl is-active coderbot.service'; }

cmd_stop() {
  require_name "${1:-}"; local name="$1"
  # Stop the agent first so it drops its in-flight tick cleanly, then deallocate so the
  # VM stops costing anything. Every disk, the NIC and the public IP stay.
  if [ "$(power_state "$name")" = "VM running" ]; then
    run_on "$name" 'systemctl stop coderbot.service' >/dev/null || true
  fi
  az vm deallocate -g "$(agent_rg "$name")" -n "$(vm_name "$name")" -o none
  echo "$name deallocated; the disk and its state are kept"
}

cmd_start() {
  require_name "${1:-}"
  az vm start -g "$(agent_rg "$1")" -n "$(vm_name "$1")" -o none
  echo "$1 starting; the container comes up on its own"
}

cmd_destroy() {
  require_name "${1:-}"; local name="$1"; shift || true
  local snap=true
  while [ $# -gt 0 ]; do case "$1" in --no-snapshot) snap=false; shift ;; *) die "unknown option $1" ;; esac; done
  local rg; rg=$(agent_rg "$name")
  if [ "$snap" = true ]; then
    local disk_id; disk_id=$(az disk show -g "$rg" -n "coderbot-$name-data" --query id -o tsv)
    local snap_name="coderbot-$name-$(date -u +%Y%m%d-%H%M%S)"
    echo "snapshotting the data disk into $SHARED_RG as $snap_name"
    az snapshot create -g "$SHARED_RG" -n "$snap_name" --source "$disk_id" -o none
  fi
  echo "deleting resource group $rg"
  az group delete -n "$rg" --yes -o none
  echo "deleted. Restore later with: deploy.sh agent $name --snapshot <snapshot id> ..."
}

# ------------------------------------------------------------------ prices
cmd_prices() {
  local location="" sizes="Standard_D2pds_v6,Standard_D2ps_v6,Standard_D2pds_v5,Standard_D2ps_v5,Standard_D2as_v5"
  while [ $# -gt 0 ]; do case "$1" in
    --location) location="$2"; shift 2 ;; --size) sizes="$2"; shift 2 ;; *) die "unknown option $1" ;;
  esac; done
  [ -n "$location" ] || location=$(shared_output location)
  echo "retail prices in $location (USD/hour, from the public Azure retail prices API)"
  python3 - "$location" "$sizes" <<'PY'
import json, sys, urllib.parse, urllib.request
location, sizes = sys.argv[1], sys.argv[2].split(",")
print(f"{'size':<24}{'spot':>10}{'pay-as-you-go':>16}{'saving':>9}")
for size in sizes:
    flt = (f"armRegionName eq '{location}' and armSkuName eq '{size}' "
           "and priceType eq 'Consumption' and serviceName eq 'Virtual Machines'")
    url = "https://prices.azure.com/api/retail/prices?" + urllib.parse.urlencode({"$filter": flt})
    try:
        items = json.load(urllib.request.urlopen(url, timeout=30)).get("Items", [])
    except Exception as err:
        print(f"{size:<24}  lookup failed: {err}")
        continue
    def pick(spot):
        vals = [i["retailPrice"] for i in items
                if ("Spot" in i.get("meterName", "")) == spot
                and "Low Priority" not in i.get("meterName", "")
                and not i.get("productName", "").endswith("Windows")]
        return min(vals) if vals else None
    spot, ondemand = pick(True), pick(False)
    if spot is None and ondemand is None:
        print(f"{size:<24}  not offered in {location}")
        continue
    saving = f"{(1 - spot / ondemand) * 100:.0f}%" if spot and ondemand else "-"
    print(f"{size:<24}{spot if spot else '-':>10}{ondemand if ondemand else '-':>16}{saving:>9}")
PY
  echo "capacity, not price, is what evicts a Spot VM when maxPrice is -1."
}

cmd="${1:-}"; shift || true
case "$cmd" in
  shared|secrets|agent|status|logs|exec|update|restart|stop|start|destroy|prices) need_tools; "cmd_$cmd" "$@" ;;
  *) sed -n '2,/^set -euo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
