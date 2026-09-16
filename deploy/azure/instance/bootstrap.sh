#!/bin/bash
# First-boot script for a coderbot agent VM (Ubuntu 24.04). Delivered as cloud-init
# custom data by deploy.sh, which also writes /etc/coderbot/agent.conf with this
# agent's parameters. Deliberately small: it installs Docker, mounts the data disk and
# clones the repo, then every later decision is made by the versioned scripts in that
# checkout, so changing them never needs a new VM.
set -euxo pipefail
exec > >(tee -a /var/log/coderbot-bootstrap.log) 2>&1

. /etc/coderbot/agent.conf
# Azure mounts the VM's ephemeral resource disk at /mnt, so the persistent disk goes
# somewhere else. /datadrive is the Azure convention.
MNT=/datadrive
export DEBIAN_FRONTEND=noninteractive
APT="apt-get -o DPkg::Lock::Timeout=900 -y"

$APT update
$APT install -y ca-certificates curl git jq gnupg
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  > /etc/apt/sources.list.d/docker.list
$APT update
$APT install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl stop docker docker.socket || true

# Headroom for the image build and for the parallel subagents a task now fans out to.
if ! swapon --show | grep -q swapfile; then
  fallocate -l 4G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

# ---- the persistent data disk --------------------------------------------------
# Attached by the template at LUN 0 and never detached: an eviction deallocates this
# VM rather than deleting it, so the disk is still here when it starts again.
DEV=/dev/disk/azure/scsi1/lun0
for _ in $(seq 1 30); do [ -e "$DEV" ] && break; sleep 2; done
[ -e "$DEV" ] || { echo "data disk not present at $DEV"; exit 1; }
if ! blkid "$DEV" >/dev/null 2>&1; then
  mkfs.ext4 -L coderbot "$DEV"
fi
mkdir -p "$MNT"
UUID=$(blkid -s UUID -o value "$DEV")
grep -q "$UUID" /etc/fstab || echo "UUID=$UUID $MNT ext4 defaults,nofail 0 2" >> /etc/fstab
systemctl daemon-reload
mount "$MNT"

# Docker's data-root on the data disk: image layers, the build cache and the target
# repo's end-to-end images all survive an eviction, so only the very first boot of an
# agent pays for a full build.
mkdir -p "$MNT/docker"
mkdir -p /etc/docker /etc/systemd/system/docker.service.d
echo '{"data-root": "/datadrive/docker"}' > /etc/docker/daemon.json
printf '[Unit]\nRequiresMountsFor=/datadrive\n' > /etc/systemd/system/docker.service.d/datadrive.conf
systemctl daemon-reload
systemctl enable --now docker

# ---- the coderbot checkout, then hand over to its own boot script ----------------
if [ ! -d "$MNT/coderbot/.git" ]; then
  git clone "$CODERBOT_REPO_URL" "$MNT/coderbot"
fi
git -C "$MNT/coderbot" fetch --tags origin
# A branch name must track the remote; tags and commits resolve as they are.
git -C "$MNT/coderbot" checkout -q --detach "origin/$CODERBOT_REF" 2>/dev/null \
  || git -C "$MNT/coderbot" checkout -q --detach "$CODERBOT_REF"

HERE="$MNT/coderbot/deploy/azure"
install -m 0755 "$HERE/instance/spot-watch.sh" /usr/local/sbin/coderbot-spot-watch
install -m 0644 "$HERE/instance/coderbot-boot.service" "$HERE/instance/coderbot.service" \
  "$HERE/instance/coderbot-spot-watch.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable coderbot-boot.service coderbot.service
systemctl enable --now coderbot-spot-watch.service
systemctl start coderbot.service
