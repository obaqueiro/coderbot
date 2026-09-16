#!/bin/bash
# Runs on every boot, before the container starts (coderbot-boot.service), and again on
# `deploy.sh update`. Idempotent: it seeds only what is missing, refreshes the per-agent
# environment, and leaves the container to coderbot.service.
set -euxo pipefail
. /etc/coderbot/agent.conf
MNT=/datadrive
BOT_UID=501   # the container runs as this uid; see the repository Dockerfile

# Key Vault through the VM managed identity: an IMDS token, then the secrets REST API.
# No Azure CLI on the box. The role assignment is created in the same deployment as the
# VM, so on a first boot it can take a couple of minutes to propagate; retry rather than
# fail the boot.
imds_token() {
  curl -fsS -H 'Metadata: true' \
    "http://169.254.169.254/metadata/identity/oauth2/token?api-version=2018-02-01&resource=https%3A%2F%2Fvault.azure.net" \
    | jq -r '.access_token // empty'
}
get_secret() {  # get_secret NAME -> value on stdout
  local name="$1" token value
  for _ in $(seq 1 30); do
    token=$(imds_token || true)
    if [ -n "$token" ]; then
      value=$(curl -fsS -H "Authorization: Bearer $token" \
        "${VAULT_URI%/}/secrets/${name}?api-version=7.4" | jq -r '.value // empty' || true)
      if [ -n "$value" ]; then printf '%s' "$value"; return 0; fi
    fi
    sleep 10
  done
  echo "could not read secret '$name' from $VAULT_URI" >&2
  return 1
}
# Credentials are seeded once and then owned by the agent: it refreshes the Google token
# and the Claude session in place, so a later boot must not overwrite them with the older
# copies in the vault. RESEED_SECRETS=true forces it.
seed() {  # seed SECRET_NAME PATH
  if [ "$RESEED_SECRETS" = "true" ] || [ ! -s "$2" ]; then
    mkdir -p "$(dirname "$2")"
    get_secret "$1" > "$2"
    chmod 600 "$2"
  fi
}

umask 077
mkdir -p "$MNT/data" "$MNT/claude" "$MNT/repo"
seed env                "$MNT/codebot.env"
seed claude-credentials "$MNT/claude/.credentials.json"
seed google-token       "$MNT/data/token.json"
seed google-credentials "$MNT/data/credentials.json"
seed claude-json        "$MNT/claude.json" || echo '{}' > "$MNT/claude.json"

# The target repo checkout: the agent's working copy, where its branch and any
# uncommitted work live between ticks.
TARGET_DIR="$MNT/repo/$(basename "$TARGET_REPO")"
if [ ! -d "$TARGET_DIR/.git" ]; then
  GH_TOKEN="$(grep -E '^GH_TOKEN=' "$MNT/codebot.env" | head -n1 | cut -d= -f2-)"
  git clone "https://x-access-token:$GH_TOKEN@github.com/$TARGET_REPO.git" "$TARGET_DIR"
  # The container authenticates with gh; keep the token out of the remote URL.
  git -C "$TARGET_DIR" remote set-url origin "https://github.com/$TARGET_REPO.git"
fi

# Per-agent overrides. Last in the compose env_file list, so they win over codebot.env.
cat > "$MNT/agent.env" <<EOA
CODEBOT_INSTANCE=$AGENT_NAME
CODEBOT_REPO_PATH=$TARGET_DIR
EOA

chown -R "$BOT_UID" "$MNT/data" "$MNT/claude" "$MNT/repo" "$MNT/coderbot"
chown "$BOT_UID" "$MNT/codebot.env" "$MNT/agent.env" "$MNT/claude.json"

# Keep the units in step with the checkout (a `deploy.sh update` may have moved the ref).
HERE="$MNT/coderbot/deploy/azure"
install -m 0755 "$HERE/instance/spot-watch.sh" /usr/local/sbin/coderbot-spot-watch
install -m 0644 "$HERE/instance/coderbot-boot.service" "$HERE/instance/coderbot.service" \
  "$HERE/instance/coderbot-spot-watch.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable coderbot-spot-watch.service
