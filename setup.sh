#!/usr/bin/env bash
# Interactive setup: walks through every .env variable codebot needs, explains
# each one, offers discoverable defaults, validates what's cheaply checkable,
# guides the Google OAuth credential setup with browser deep links, and
# writes the result to .env. See README.md "One-time setup" for context.
set -euo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

ENV_FILE=".env"
CREDENTIALS_PATH="data/credentials.json"
TOKEN_PATH="data/token.json"
OPENCODE_VERSION="1.18.18"
HAS_EXISTING_ENV="no"
KEEP_EXISTING_ENV="no"

echo "== codebot setup =="
echo "This walks through the values codebot needs and writes them to $ENV_FILE."

if [ -f "$ENV_FILE" ]; then
  HAS_EXISTING_ENV="yes"
fi

# existing_value VAR: VAR's value in the current .env, or "" if absent/no .env.
# A plain function (not an associative array) so this runs on bash 3.2, which
# macOS ships as /bin/bash and has no declare -A.
existing_value() {
  local var="$1" line
  [ "$HAS_EXISTING_ENV" = "yes" ] || return 0
  while IFS= read -r line; do
    case "$line" in
    "$var"=*) printf '%s' "${line#"$var"=}" ;;
    esac
  done <"$ENV_FILE"
}

# print_current_settings: show the configuration a review will start from
# without ever exposing credentials in the terminal.
print_current_settings() {
  local var value
  echo "Current non-secret settings:"
  for var in CODEBOT_REPO_PATH CODEBOT_DOC_ID CODEBOT_PROJECT_NAME GIT_AUTHOR_NAME \
    GIT_AUTHOR_EMAIL CODEBOT_AGENT CLAUDE_MODEL OPENCODE_PROVIDER OPENCODE_MODEL \
    CODEBOT_USER_EMAIL CODEBOT_LOG_LEVEL CODEBOT_QUALITY_GATE_MAX_ROUNDS \
    CODEBOT_ARCHIVE_MAX_ROUNDS CODEBOT_BASE_BRANCH; do
    value="$(existing_value "$var")"
    if [ -n "$value" ]; then
      echo "  - $var=$value"
    else
      echo "  - $var=(not set)"
    fi
  done
  for var in GH_TOKEN CLAUDE_CODE_OAUTH_TOKEN CLAUDE_API_KEY; do
    if [ -n "$(existing_value "$var")" ]; then
      echo "  - $var=(set; value hidden)"
    else
      echo "  - $var=(not set)"
    fi
  done
}

# prepare_env_write: require the user to approve replacing an existing .env
# before interactive authentication can create or update credentials.
prepare_env_write() {
  local backup n confirm
  [ "$HAS_EXISTING_ENV" = "yes" ] || return 0

  backup="$ENV_FILE.bak.1"
  n=1
  while [ -e "$backup" ]; do
    n=$((n + 1))
    backup="$ENV_FILE.bak.$n"
  done

  echo
  read -r -p "$ENV_FILE will be replaced. Back it up to $backup and continue? [y/N] " confirm
  if [[ ! "$confirm" =~ ^[Yy] ]]; then
    echo "Aborted — $ENV_FILE was left unchanged."
    exit 1
  fi
  cp "$ENV_FILE" "$backup"
  echo "Backed up existing $ENV_FILE to $backup."
}

# prompt_var VAR "explanation" "default" [secret: yes/no]
# Prints the explanation and prompt to stderr; prints the resolved value (and
# ONLY the resolved value) to stdout, so callers can do `x=$(prompt_var ...)`.
# For secret=yes, neither an existing/detected default nor the typed value is
# ever shown on the terminal.
prompt_var() {
  local var="$1" explanation="$2" default="$3" secret="${4:-no}"
  local current
  current="$(existing_value "$var")"
  local use_default="$default"
  [ -n "$current" ] && use_default="$current"

  echo >&2
  echo "$explanation" >&2
  local label="$var"
  if [ -n "$use_default" ]; then
    if [ "$secret" = "yes" ]; then
      label="$label [Enter to keep the current value]"
    else
      label="$label [$use_default]"
    fi
  fi

  local value
  if [ "$secret" = "yes" ]; then
    read -r -s -p "$label: " value
    echo >&2
  else
    read -r -p "$label: " value
  fi
  [ -z "$value" ] && value="$use_default"
  echo "$value"
}

# open_url URL: prints the URL (always, so a headless session can copy it
# manually) and best-effort opens it in a browser via `open` (macOS) or
# `xdg-open` (Linux); never fails setup if neither is available.
open_url() {
  local url="$1"
  echo "  $url"
  if command -v open >/dev/null 2>&1; then
    open "$url" >/dev/null 2>&1 &
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$url" >/dev/null 2>&1 &
  fi
}

VENV_DIR=".venv"
PYTHON_BIN="python3"

# ensure_oauth_deps: install requirements.txt into an isolated venv ($VENV_DIR),
# sets PYTHON_BIN to the interpreter to run setup_oauth.py with. A venv sidesteps
# "externally-managed-environment" pip refusals on modern macOS/Linux system
# Pythons. Falls back to installing against the system python3 (pip3/pip/`python3
# -m pip`, whichever works) only if creating the venv itself isn't possible.
# Returns non-zero only if every strategy failed.
ensure_oauth_deps() {
  if [ ! -x "$VENV_DIR/bin/python" ]; then
    echo "Creating a Python virtual environment for codebot's host-side dependencies ($VENV_DIR)..."
    if ! python3 -m venv "$VENV_DIR" >/dev/null 2>&1; then
      echo "  -> could not create $VENV_DIR; falling back to the system python3." >&2
      rm -rf "$VENV_DIR"
    fi
  fi

  if [ -x "$VENV_DIR/bin/python" ]; then
    PYTHON_BIN="$VENV_DIR/bin/python"
    echo "Installing Python dependencies into $VENV_DIR..."
    if "$PYTHON_BIN" -m pip install -r requirements.txt >/dev/null 2>&1; then
      return 0
    fi
    echo "  -> pip install into $VENV_DIR failed." >&2
  fi

  PYTHON_BIN="python3"
  echo "Installing Python dependencies for the system python3..."
  if python3 -m pip --version >/dev/null 2>&1 && python3 -m pip install --user -r requirements.txt >/dev/null 2>&1; then
    return 0
  fi
  if command -v pip3 >/dev/null 2>&1 && pip3 install --user -r requirements.txt >/dev/null 2>&1; then
    return 0
  fi
  if command -v pip >/dev/null 2>&1 && pip install --user -r requirements.txt >/dev/null 2>&1; then
    return 0
  fi
  return 1
}

# Run OpenCode in Codebot's image. Its credentials are written through the
# bind-mounted /app/data directory, never to the host user's OpenCode config.
run_opencode() {
  docker compose run --rm --no-deps \
    --entrypoint opencode \
    -e XDG_DATA_HOME=/app/data/opencode/data \
    -e XDG_CONFIG_HOME=/app/data/opencode/config \
    -e XDG_CACHE_HOME=/app/data/opencode/cache \
    -e XDG_STATE_HOME=/app/data/opencode/state \
    codebot "$@"
}

ensure_opencode() {
  if ! docker compose version >/dev/null 2>&1; then
    echo "  -> Docker Compose is required to run OpenCode authentication in the Codebot image." >&2
    return 1
  fi
  echo "Building the Codebot image with OpenCode $OPENCODE_VERSION..."
  docker compose build codebot
}

# ------------------------------------------------ existing configuration

if [ "$HAS_EXISTING_ENV" = "yes" ]; then
  echo
  echo "Found an existing $ENV_FILE."
  print_current_settings
  echo "Press Enter at a prompt to keep its current value."
  read -r -p "Review and modify these settings? [Y/n] " review_answer
  if [[ "$review_answer" =~ ^[Nn] ]]; then
    KEEP_EXISTING_ENV="yes"
    echo "Keeping $ENV_FILE unchanged."
  else
    prepare_env_write
  fi
fi

if [ "$KEEP_EXISTING_ENV" = "no" ]; then

  # -------------------------------------------------------------- required

  while true; do
    CODEBOT_REPO_PATH=$(prompt_var "CODEBOT_REPO_PATH" \
      "Absolute path to the target project's git checkout that codebot will work on and open PRs against." \
      "")
    case "$CODEBOT_REPO_PATH" in
    /*) ;;
    *)
      echo "  -> must be an absolute path (starting with /)." >&2
      continue
      ;;
    esac
    if [ ! -e "$CODEBOT_REPO_PATH/.git" ]; then
      echo "  -> $CODEBOT_REPO_PATH is not a git checkout (no .git found there)." >&2
      continue
    fi
    break
  done

  while true; do
    raw=$(prompt_var "CODEBOT_DOC_ID" \
      "Google Doc id of the improvements backlog. Open the Doc and copy the id from its URL (.../document/d/<ID>/edit) — pasting the full URL also works." \
      "")
    if [[ "$raw" =~ /document/d/([a-zA-Z0-9_-]+) ]]; then
      CODEBOT_DOC_ID="${BASH_REMATCH[1]}"
    else
      CODEBOT_DOC_ID="$raw"
    fi
    if [[ -n "$CODEBOT_DOC_ID" && "$CODEBOT_DOC_ID" =~ ^[a-zA-Z0-9_-]+$ ]]; then
      break
    fi
    echo "  -> that doesn't look like a valid Doc id or URL." >&2
  done

  while true; do
    CODEBOT_USER_EMAIL=$(prompt_var "CODEBOT_USER_EMAIL" \
      "The email address codebot sends its updates to and reads your replies from." \
      "")
    if [[ "$CODEBOT_USER_EMAIL" =~ ^[^[:space:]@]+@[^[:space:]@]+\.[^[:space:]@]+$ ]]; then
      break
    fi
    echo "  -> that doesn't look like a valid email address." >&2
  done

  gh_token_default=""
  gh_note="Create one at https://github.com/settings/tokens, or run: gh auth login"
  if command -v gh >/dev/null 2>&1; then
    gh_token_default="$(gh auth token 2>/dev/null || true)"
    [ -n "$gh_token_default" ] && gh_note="Detected via the authenticated gh CLI — press Enter to use it."
  fi
  GH_TOKEN=$(prompt_var "GH_TOKEN" \
    "GitHub personal access token with 'repo' scope; used for git pushes over HTTPS and gh pr/api calls. $gh_note" \
    "$gh_token_default" "yes")

  while true; do
    CODEBOT_AGENT=$(prompt_var "CODEBOT_AGENT" \
      "Coding CLI codebot uses: enter 'claude' to use Claude Code, or 'opencode' to use OpenCode and its provider login flow." \
      "claude")
    if [ "$CODEBOT_AGENT" = "claude" ] || [ "$CODEBOT_AGENT" = "opencode" ]; then
      break
    fi
    echo "  -> enter 'claude' or 'opencode'." >&2
  done

  CLAUDE_CODE_OAUTH_TOKEN=""
  CLAUDE_MODEL=""
  OPENCODE_PROVIDER=""
  OPENCODE_MODEL=""
  if [ "$CODEBOT_AGENT" = "claude" ]; then
    CLAUDE_CODE_OAUTH_TOKEN=$(prompt_var "CLAUDE_CODE_OAUTH_TOKEN" \
      "OAuth token for headless Claude Code runs inside the container (macOS keeps Claude credentials in the Keychain, which the Linux container can't read). Run 'claude setup-token' yourself — it opens a browser — then paste its output here." \
      "" "yes")
  else
    if ! ensure_opencode; then
      echo "OpenCode setup cannot continue until the Codebot image can be built." >&2
      exit 1
    fi
    while true; do
      OPENCODE_PROVIDER=$(prompt_var "OPENCODE_PROVIDER" \
        "OpenCode provider ID to authenticate (for example: openai, github-copilot, gitlab, or opencode). OpenCode will show that provider's browser, device-code, or API-key methods." \
        "")
      [ -n "$OPENCODE_PROVIDER" ] && break
      echo "  -> a provider ID is required." >&2
    done
    echo "Starting OpenCode authentication for '$OPENCODE_PROVIDER' in the Codebot container. Complete any browser or device-code flow, then return here."
    run_opencode auth login --provider "$OPENCODE_PROVIDER"
    if ! run_opencode auth list | grep -qi "$OPENCODE_PROVIDER"; then
      echo "  -> OpenCode did not report an authenticated '$OPENCODE_PROVIDER' provider." >&2
      exit 1
    fi
    echo "Authenticated models for '$OPENCODE_PROVIDER':"
    run_opencode models "$OPENCODE_PROVIDER" || true
    while true; do
      OPENCODE_MODEL=$(prompt_var "OPENCODE_MODEL" \
        "Choose the fully-qualified model codebot should use, in provider/model form. Select one from the list above." \
        "")
      if [[ "$OPENCODE_MODEL" == "$OPENCODE_PROVIDER"/* && "$OPENCODE_MODEL" != */ ]]; then
        break
      fi
      echo "  -> model must start with $OPENCODE_PROVIDER/ and include a model ID." >&2
    done
  fi

  git_name_default="$(git config --global user.name 2>/dev/null || true)"
  GIT_AUTHOR_NAME=$(prompt_var "GIT_AUTHOR_NAME" \
    "Git author name codebot commits as in the target repo." \
    "${git_name_default:-codebot}")

  git_email_default="$(git config --global user.email 2>/dev/null || true)"
  GIT_AUTHOR_EMAIL=$(prompt_var "GIT_AUTHOR_EMAIL" \
    "Git author email codebot commits as in the target repo." \
    "${git_email_default:-codebot@example.com}")

  # -------------------------------------------------------------- optional

  repo_basename="$(basename "$CODEBOT_REPO_PATH")"
  CODEBOT_PROJECT_NAME=$(prompt_var "CODEBOT_PROJECT_NAME" \
    "Optional: human-readable project name used in prompts. Leave blank to default to the repo directory name ($repo_basename)." \
    "")

  if [ "$CODEBOT_AGENT" = "claude" ]; then
    CLAUDE_MODEL=$(prompt_var "CLAUDE_MODEL" \
      "Optional: Claude model codebot uses for its working sessions." \
      "claude-opus-4-8")
  fi

  CODEBOT_BASE_BRANCH=$(prompt_var "CODEBOT_BASE_BRANCH" \
    "Optional: the branch codebot treats as the trunk — it syncs from this branch before picking a task, branches feature work off of it, opens PRs against it, and resets to it on abort." \
    "main")

  CODEBOT_LOG_LEVEL=$(prompt_var "CODEBOT_LOG_LEVEL" \
    "Optional: log verbosity — DEBUG traces every email, video, and git call; INFO is quieter." \
    "DEBUG")

  while true; do
    CODEBOT_QUALITY_GATE_MAX_ROUNDS=$(prompt_var "CODEBOT_QUALITY_GATE_MAX_ROUNDS" \
      "Optional: maximum verification/internal-review repair rounds." \
      "3")
    [[ "$CODEBOT_QUALITY_GATE_MAX_ROUNDS" =~ ^[1-9][0-9]*$ ]] && break
    echo "  -> must be a positive integer." >&2
  done

  while true; do
    CODEBOT_ARCHIVE_MAX_ROUNDS=$(prompt_var "CODEBOT_ARCHIVE_MAX_ROUNDS" \
      "Optional: maximum OpenSpec archive repair rounds." \
      "3")
    [[ "$CODEBOT_ARCHIVE_MAX_ROUNDS" =~ ^[1-9][0-9]*$ ]] && break
    echo "  -> must be a positive integer." >&2
  done

  CLAUDE_API_KEY=""
  if [ "$CODEBOT_AGENT" = "claude" ]; then
    CLAUDE_API_KEY=$(prompt_var "CLAUDE_API_KEY" \
      "Optional: only needed if the target repo has its own .claude/settings.json with an 'apiKeyHelper' that reads this variable. Claude Code always tries apiKeyHelper before CLAUDE_CODE_OAUTH_TOKEN with no fallback, so a target repo's own apiKeyHelper convention can break codebot's OAuth auth unless this is set to a real key from https://console.anthropic.com/ (billed per-token, separate from your subscription plan). Leave blank if the target repo has no such setting." \
      "" "yes")
  fi
fi

# ---------------------------------------------------------------- Google OAuth

echo
echo "== Google OAuth setup =="
if [ -f "$TOKEN_PATH" ]; then
  echo "Found $TOKEN_PATH — Google OAuth is already configured."
  echo "(Delete it and re-run this script if you need to redo this.)"
else
  if [ -f "$CREDENTIALS_PATH" ]; then
    echo "Found $CREDENTIALS_PATH already — skipping credential creation."
  else
    echo "Codebot needs a Google OAuth client to read/update the backlog Doc,"
    echo "read Drive, and send email. First, a browser window will open to the"
    echo "Google Cloud Console so you can create or select a project."
    read -r -p "Press Enter to continue..." _
    open_url "https://console.cloud.google.com/projectselector2/home/dashboard"
    echo "In the page that opened: create a new project, or select an existing one."
    read -r -p "Press Enter once you've selected a project..." _

    echo
    echo "Next, we'll enable the three APIs codebot needs. A browser tab will"
    echo "open for each one."
    read -r -p "Press Enter to continue..." _
    for api in gmail docs drive; do
      open_url "https://console.cloud.google.com/apis/library/${api}.googleapis.com"
    done
    echo "On each tab that opened: click 'Enable' (if it already says 'Manage', it's already enabled)."
    read -r -p "Press Enter once all three APIs are enabled..." _

    echo
    echo "Finally, we'll create the OAuth client. A browser tab will open to the"
    echo "Credentials page."
    read -r -p "Press Enter to continue..." _
    open_url "https://console.cloud.google.com/apis/credentials"
    echo "On that page: '+ Create Credentials' -> 'OAuth client ID' -> Application"
    echo "type 'Desktop app' -> give it a name -> Create, then click 'Download JSON'."
    while true; do
      read -r -p "Copy the downloaded file to $CREDENTIALS_PATH, then press Enter (or type 'skip' to do this later): " ack
      if [ "$ack" = "skip" ]; then
        echo "Skipping — place the file at $CREDENTIALS_PATH and run this script again, or run 'python3 setup_oauth.py' yourself."
        break
      fi
      if [ -f "$CREDENTIALS_PATH" ]; then
        break
      fi
      echo "  -> $CREDENTIALS_PATH not found yet." >&2
    done
  fi

  if [ -f "$CREDENTIALS_PATH" ]; then
    echo
    echo "Running the consent flow — one more browser window will open to sign in and grant access."
    if ensure_oauth_deps; then
      "$PYTHON_BIN" setup_oauth.py || echo "  -> setup_oauth.py did not complete; re-run it yourself once ready: $PYTHON_BIN setup_oauth.py"
    else
      cat <<MSG
  -> could not install Python dependencies automatically. Install them yourself, e.g.:
       python3 -m venv $VENV_DIR && $VENV_DIR/bin/python -m pip install -r requirements.txt
     then run: $VENV_DIR/bin/python setup_oauth.py
MSG
    fi
  fi
fi

# ---------------------------------------------------------------- write .env

if [ "$KEEP_EXISTING_ENV" = "yes" ]; then
  echo
  echo "$ENV_FILE was left unchanged."
else
  cat >"$ENV_FILE" <<EOF
CODEBOT_REPO_PATH=$CODEBOT_REPO_PATH
CODEBOT_DOC_ID=$CODEBOT_DOC_ID
CODEBOT_PROJECT_NAME=$CODEBOT_PROJECT_NAME
GH_TOKEN=$GH_TOKEN
GIT_AUTHOR_NAME=$GIT_AUTHOR_NAME
GIT_AUTHOR_EMAIL=$GIT_AUTHOR_EMAIL
CODEBOT_AGENT=$CODEBOT_AGENT
CLAUDE_CODE_OAUTH_TOKEN=$CLAUDE_CODE_OAUTH_TOKEN
CLAUDE_MODEL=$CLAUDE_MODEL
OPENCODE_PROVIDER=$OPENCODE_PROVIDER
OPENCODE_MODEL=$OPENCODE_MODEL
CODEBOT_USER_EMAIL=$CODEBOT_USER_EMAIL
CODEBOT_LOG_LEVEL=$CODEBOT_LOG_LEVEL
CODEBOT_QUALITY_GATE_MAX_ROUNDS=$CODEBOT_QUALITY_GATE_MAX_ROUNDS
CODEBOT_ARCHIVE_MAX_ROUNDS=$CODEBOT_ARCHIVE_MAX_ROUNDS
CODEBOT_BASE_BRANCH=$CODEBOT_BASE_BRANCH
CLAUDE_API_KEY=$CLAUDE_API_KEY
EOF
  chmod 600 "$ENV_FILE"

  echo
  echo "Wrote $ENV_FILE with:"
  for var in CODEBOT_REPO_PATH CODEBOT_DOC_ID CODEBOT_PROJECT_NAME GH_TOKEN GIT_AUTHOR_NAME \
    GIT_AUTHOR_EMAIL CODEBOT_AGENT CLAUDE_CODE_OAUTH_TOKEN CLAUDE_MODEL OPENCODE_PROVIDER \
    OPENCODE_MODEL CODEBOT_USER_EMAIL CODEBOT_LOG_LEVEL CODEBOT_QUALITY_GATE_MAX_ROUNDS \
    CODEBOT_ARCHIVE_MAX_ROUNDS CODEBOT_BASE_BRANCH CLAUDE_API_KEY; do
    echo "  - $var"
  done
fi

echo
echo "Next: docker compose up -d --build"
