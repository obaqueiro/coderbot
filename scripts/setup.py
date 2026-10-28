"""Guided configuration entrypoint, with an optional Textual terminal interface."""
import argparse
import base64
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from scripts.setup_catalog import (BY_KEY, GITHUB_TOKEN_URL, JIRA_STATUS_KEYS, SETTINGS,
                                   SLACK_APP_SETTINGS_URL, SELF_HEAL_OPTIONS, SELF_HEAL_SETUP_HELP,
                                   effective, normalize_value, relevant, validate, validate_value)
from scripts.setup_env import EnvFile

ROOT = Path(__file__).resolve().parent.parent
PRIMARY_PAGES = ("Repository", "Backlog", "Conversation", "Agent", "Evidence", "Self-healing")
VM_HOST_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*@[A-Za-z0-9][A-Za-z0-9.-]*\Z")

# Sent to `ssh ... sh -eu` on stdin, never interpolated with user input. Recreate
# rather than merely restarting so an initial copy's VM-adjusted .env takes effect.
_VM_RESTART_AND_CHECK = """app="$HOME/codebot/app"
set -- -f "$app/docker-compose.yml"
if [ -f "$app/docker-compose.override.yml" ]; then
    set -- "$@" -f "$app/docker-compose.override.yml"
fi
docker compose --project-directory "$app" "$@" up -d --force-recreate codebot
container=$(docker compose --project-directory "$app" "$@" ps -q codebot)
if [ -z "$container" ]; then
    echo 'Docker Compose did not start codebot' >&2
    exit 1
fi
attempt=0
while [ "$attempt" -lt 90 ]; do
    health=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container")
    if [ "$health" = healthy ]; then
        echo 'Coderbot is healthy on the VM.'
        exit 0
    fi
    if [ "$health" = unhealthy ] || [ "$health" = exited ] || [ "$health" = dead ]; then
        echo "Coderbot did not become healthy (status: $health)" >&2
        exit 1
    fi
    attempt=$((attempt + 1))
    sleep 3
done
echo 'Coderbot did not become healthy within 270 seconds' >&2
exit 1
"""


def sync_to_vm(host: str, *, code_only: bool, restart: bool) -> str:
    """Run the existing copier; optionally recreate and check the remote bot."""
    host = host.strip()
    if not VM_HOST_RE.fullmatch(host):
        raise ValueError("Enter an SSH host as user@host (for example azureuser@74.235.122.91)")
    command = ["bash", str(ROOT / "scripts/sync-to-vm.sh"), host]
    if code_only:
        command.append("--code-only")
    try:
        subprocess.run(command, cwd=ROOT, check=True)
    except (OSError, subprocess.CalledProcessError) as error:
        raise RuntimeError(f"VM copy failed: {error}") from error
    if restart:
        try:
            subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                            host, "sh -eu"], input=_VM_RESTART_AND_CHECK, text=True,
                           cwd=ROOT, check=True, timeout=330)
        except (OSError, subprocess.SubprocessError) as error:
            raise RuntimeError(f"Files copied, but VM restart/health check failed: {error}") from error
    return ("VM copy completed and Coderbot is healthy." if restart else
            "VM copy completed. Restart the remote bot to load the new code.")

# Universal, version-pinned PyPI wheels. Direct wheels avoid slow/broken simple-
# index metadata lookups on hosts with stale private pip index credentials.
_SETUP_WHEELS = (
    "https://files.pythonhosted.org/packages/fb/be/35261223d9416a0751cdff1c7b4a6f881387218a12d439fe22fefebc8c04/textual-8.2.8-py3-none-any.whl",
    "https://files.pythonhosted.org/packages/b3/81/4da04ced5a082363ecfa159c010d200ecbd959ae410c10c0264a38cac0f5/markdown_it_py-4.2.0-py3-none-any.whl",
    "https://files.pythonhosted.org/packages/a5/69/6da5581c6a7fede7dc261bf4e67d6adca4196f176b43288b55b3db395b6e/mdit_py_plugins-0.6.1-py3-none-any.whl",
    "https://files.pythonhosted.org/packages/40/31/1521c38d175108ac395941adbcb9f11e085340bed81ad166e8ad8c21c17b/platformdirs-4.12.1-py3-none-any.whl",
    "https://files.pythonhosted.org/packages/71/46/17f022dd3e953bf20a04a028a21ec746d942f8d2af30fa0f124fa0e6a684/pygments-2.21.0-py3-none-any.whl",
    "https://files.pythonhosted.org/packages/82/3b/64d4899d73f91ba49a8c18a8ff3f0ea8f1c1d75481760df8c68ef5235bf5/rich-15.0.0-py3-none-any.whl",
    "https://files.pythonhosted.org/packages/b3/38/89ba8ad64ae25be8de66a6d463314cf1eb366222074cfda9ee839c56a4b4/mdurl-0.1.2-py3-none-any.whl",
    "https://files.pythonhosted.org/packages/13/d4/1152d1c7ab42d8b908be64fd200ddc870dc9d4925e951198702084aa1a7d/linkify_it_py-2.2.0-py3-none-any.whl",
    "https://files.pythonhosted.org/packages/61/73/d21edf5b204d1467e06500080a50f79d49ef2b997c79123a536d4a17d97c/uc_micro_py-2.0.0-py3-none-any.whl",
    "https://files.pythonhosted.org/packages/49/d3/b8441a820a491ddfc024b0b0cf0393375b75ea13866d9c66727e54c2fc80/typing_extensions-4.16.0-py3-none-any.whl",
)


def fields_for(page: str, values: dict[str, str], search: str = ""):
    if page == "Repository":
        return [s for s in SETTINGS if s.group == "Repository" and not s.advanced]
    if page == "Backlog":
        return [s for s in SETTINGS if s.key == "CODEBOT_TASK_SOURCE" or
                (s.group in ("GDoc", "GitHub", "Jira") and relevant(s, values) and not s.advanced)]
    if page == "Conversation":
        return [s for s in SETTINGS if s.key == "CODEBOT_COMM_CHANNEL" or
                (s.group in ("Email", "Slack") and relevant(s, values) and not s.advanced)]
    if page == "Agent":
        return [s for s in SETTINGS if s.group == "Agent" or
                (s.group in ("Claude", "OpenCode") and relevant(s, values) and not s.advanced)]
    if page == "Evidence":
        return [s for s in SETTINGS if s.group == "Evidence" and not s.advanced]
    if page == "Self-healing":
        return [s for s in SETTINGS if s.group == "Self-healing" and not s.advanced]
    if page == "Advanced":
        return [s for s in SETTINGS if search.casefold() in (s.key + " " + s.help).casefold()]
    return []


def _help_for(values: dict[str, str]) -> str:
    source = values.get("CODEBOT_TASK_SOURCE") or "gdoc"
    channel = values.get("CODEBOT_COMM_CHANNEL") or "email"
    agent = values.get("CODEBOT_AGENT") or "claude"
    lines = ["GH_TOKEN is required for GitHub PRs in every combination."]
    if source == "gdoc":
        lines.append("GDoc: create a Google Desktop OAuth client and enable Docs and Drive APIs.")
    elif source == "github":
        lines.append("GitHub Projects: GH_TOKEN also needs project write permission.")
    else:
        lines.append("Jira: https://id.atlassian.com/manage-profile/security/api-tokens")
        lines.append("Jira tasks must have both the selected eligible status and "
                     "CODEBOT_JIRA_PICK_LABEL; add the label to issues you want Coderbot to pick.")
    if channel == "email":
        lines.append("Email: enable the Gmail API and use scripts/setup_oauth.py for consent.")
    else:
        lines.append("Slack: create a separate app per bot, install it in the public channel, "
                     "enable Socket Mode (connections:write) and subscribe to message.channels "
                     "with channels:history, channels:read, chat:write and files:write bot scopes.")
        lines.append(f"Slack app settings and token creation: {SLACK_APP_SETTINGS_URL}")
    if (values.get("CODEBOT_EVIDENCE_UPLOAD") or "on").lower() not in ("off", "false", "0", "no"):
        lines.append("Drive evidence upload is on: enable Drive API and complete Google OAuth consent.")
    if agent == "opencode":
        lines.append("After saving: run OpenCode provider login in the container (Docker Compose).")
    else:
        lines.append("Claude: run `claude setup-token` on the host and enter its result.")
    return "\n".join(lines)


def _post_save_auth(values: dict[str, str]):
    google_needed = ((values.get("CODEBOT_TASK_SOURCE") or "gdoc") == "gdoc" or
                     (values.get("CODEBOT_COMM_CHANNEL") or "email") == "email" or
                     (values.get("CODEBOT_EVIDENCE_UPLOAD") or "on").lower()
                     not in ("off", "false", "0", "no"))
    if google_needed:
        token = ROOT / "data/token.json"
        credentials = ROOT / "data/credentials.json"
        scopes = _google_scopes(values)
        google_apis = (["gmail"] if any("gmail" in scope for scope in scopes) else []) + \
                      (["docs"] if any("documents" in scope for scope in scopes) else []) + \
                      (["drive"] if any("drive" in scope for scope in scopes) else [])
        print("Google APIs to enable: " + ", ".join(google_apis))
        for api in google_apis:
            print(f"  https://console.cloud.google.com/apis/library/{api}.googleapis.com")
        if token.exists():
            try:
                granted = set(json.loads(token.read_text()).get("scopes") or [])
            except (OSError, ValueError):
                granted = set()
            needed = set(_google_scopes(values))
            if not needed.issubset(granted):
                print("Google token lacks scopes for the selected modes; run "
                      "`python3 scripts/setup_oauth.py` to re-consent.")
            else:
                print(f"Google token already present at {token} with the required scopes.")
        elif credentials.exists():
            print("Google credentials found; run `python3 scripts/setup_oauth.py` to authorize required scopes.")
        else:
            print(f"Google OAuth client needed: save Desktop app credentials to {credentials}, "
                  "then run `python3 scripts/setup_oauth.py`.")
            print("  Create the Desktop OAuth client: https://console.cloud.google.com/apis/credentials")
        if credentials.exists() and sys.stdin.isatty() and _confirm("Run Google consent flow now?"):
            try:
                python = ROOT / ".venv/bin/python"
                if not python.exists():
                    subprocess.run([sys.executable, "-m", "venv", str(ROOT / ".venv")],
                                   check=True, timeout=60)
                subprocess.run([str(python), "-m", "pip", "install", "-r",
                                str(ROOT / "requirements.txt"), "--index-url", "https://pypi.org/simple",
                                "--timeout", "10", "--retries", "1"],
                               check=True, timeout=120)
                subprocess.run([str(python), "scripts/setup_oauth.py"], cwd=ROOT, check=True)
            except (OSError, subprocess.SubprocessError) as error:
                print(f"Google consent did not complete: {error}. "
                      "Run `python3 scripts/setup_oauth.py` after installing requirements.txt.")
    if (values.get("CODEBOT_AGENT") or "claude") == "opencode":
        provider = values.get("OPENCODE_PROVIDER", "")
        docker_auth = ["docker", "compose", "run", "--rm", "--no-deps",
                       "--entrypoint", "opencode", "-e", "XDG_DATA_HOME=/app/data/opencode/data",
                       "-e", "XDG_CONFIG_HOME=/app/data/opencode/config", "-e",
                       "XDG_CACHE_HOME=/app/data/opencode/cache", "-e",
                       "XDG_STATE_HOME=/app/data/opencode/state", "codebot", "auth", "login",
                       "--provider", provider]
        print(f"OpenCode provider {provider}: authenticate inside the container using "
              "the guided browser/device-code/API-key flow.")
        if sys.stdin.isatty() and shutil.which("docker") and _confirm("Build the image and log in now?"):
            try:
                subprocess.run(["docker", "compose", "build", "codebot"], cwd=ROOT, check=True)
                subprocess.run(docker_auth, cwd=ROOT, check=True)
            except (OSError, subprocess.SubprocessError) as error:
                print(f"OpenCode login did not complete: {error}; retry the provider login later.")
        else:
            print("When ready, build with `docker compose build codebot` and authenticate "
                  "the selected OpenCode provider in data/opencode/ via the container.")


def _confirm(prompt: str) -> bool:
    try:
        return input(prompt + " [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def _google_scopes(values: dict[str, str]) -> list[str]:
    scopes = []
    if (values.get("CODEBOT_COMM_CHANNEL") or "email") == "email":
        scopes.append("https://www.googleapis.com/auth/gmail.modify")
    if (values.get("CODEBOT_TASK_SOURCE") or "gdoc") == "gdoc":
        scopes.append("https://www.googleapis.com/auth/documents")
    if ((values.get("CODEBOT_TASK_SOURCE") or "gdoc") == "gdoc" or
            (values.get("CODEBOT_EVIDENCE_UPLOAD") or "on").lower() not in ("off", "false", "0", "no")):
        scopes.append("https://www.googleapis.com/auth/drive")
    return scopes


def _get_json(url: str, authorization: str, *, method="GET", payload: dict | None = None,
              include_scopes: bool = False):
    headers = {"Authorization": authorization, "Accept": "application/json"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, method=method, headers=headers,
                                 data=json.dumps(payload).encode() if payload is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=12) as response:
            result = json.loads(response.read())
            if include_scopes and isinstance(result, dict):
                headers = getattr(response, "headers", None)
                scopes = headers.get("x-oauth-scopes") if headers else None
                if scopes:
                    result["_granted_scopes"] = scopes
            return result
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"HTTP {error.code}") from error
    except (OSError, ValueError) as error:
        raise RuntimeError(f"connection failed: {error}") from error


def _jira_connection(values: dict[str, str]) -> tuple[str, str]:
    for name in ("CODEBOT_JIRA_URL", "CODEBOT_JIRA_EMAIL", "CODEBOT_JIRA_API_TOKEN"):
        value = values.get(name, "").strip()
        if not value:
            raise RuntimeError(f"Enter {name} before loading Jira projects")
        if problem := validate_value(BY_KEY[name], value):
            raise RuntimeError(f"{name}: {problem}")
    site = values["CODEBOT_JIRA_URL"].rstrip("/")
    token = base64.b64encode((values["CODEBOT_JIRA_EMAIL"] + ":" +
                              values["CODEBOT_JIRA_API_TOKEN"]).encode()).decode()
    return site, "Basic " + token


def fetch_jira_projects(values: dict[str, str]) -> list[dict[str, str]]:
    """List visible Jira Cloud projects by name and short key, across all pages."""
    site, auth = _jira_connection(values)
    _get_json(f"{site}/rest/api/3/myself", auth)
    projects = {}
    start = 0
    while True:
        url = f"{site}/rest/api/3/project/search?" + urllib.parse.urlencode(
            {"startAt": start, "maxResults": 50})
        page = _get_json(url, auth)
        if not isinstance(page, dict) or not isinstance(page.get("values"), list):
            raise RuntimeError("Jira returned an invalid project list")
        for project in page["values"]:
            if isinstance(project, dict) and project.get("key") and project.get("name"):
                projects[project["key"]] = {"key": project["key"], "name": project["name"]}
        next_start = page.get("startAt", start) + page.get("maxResults", 50)
        if page.get("isLast") is True or (page.get("total") is not None and
                                           next_start >= page["total"]) or not page["values"]:
            break
        if page.get("total") is None and not page.get("nextPage"):
            break
        if next_start <= start:
            raise RuntimeError("Jira repeated its project pagination position")
        start = next_start
    if not projects:
        raise RuntimeError("No Jira projects are visible to this account; check Browse Projects permission")
    return sorted(projects.values(), key=lambda item: (item["name"].casefold(), item["key"]))


def fetch_jira_statuses(values: dict[str, str]) -> list[str]:
    """Read actual workflow statuses from the configured Jira Cloud project."""
    site, auth = _jira_connection(values)
    if not values.get("CODEBOT_JIRA_PROJECT_KEY", "").strip():
        raise RuntimeError("Select CODEBOT_JIRA_PROJECT_KEY before loading Jira statuses")
    key = urllib.parse.quote(values["CODEBOT_JIRA_PROJECT_KEY"], safe="")
    _get_json(f"{site}/rest/api/3/myself", auth)
    _get_json(f"{site}/rest/api/3/project/{key}", auth)
    response = _get_json(f"{site}/rest/api/3/project/{key}/statuses", auth)
    if not isinstance(response, list):
        raise RuntimeError("Jira returned an invalid project status list")
    names = {}
    for issue_type in response:
        if not isinstance(issue_type, dict):
            continue
        for status in issue_type.get("statuses", []):
            if isinstance(status, dict) and isinstance(status.get("name"), str):
                name = status["name"].strip()
                if name:
                    names.setdefault(name.casefold(), name)
    if not names:
        raise RuntimeError(f"No workflow statuses found for Jira project {values['CODEBOT_JIRA_PROJECT_KEY']}")
    return sorted(names.values(), key=str.casefold)


def fetch_slack_channels(values: dict[str, str]) -> list[dict[str, str]]:
    """Validate this instance's tokens, then list public channels its bot joined."""
    for key in ("CODEBOT_SLACK_BOT_TOKEN", "CODEBOT_SLACK_APP_TOKEN"):
        token = values.get(key, "").strip()
        if not token:
            raise RuntimeError(f"Enter {key} before loading Slack channels")
        if problem := validate_value(BY_KEY[key], token):
            raise RuntimeError(f"{key}: {problem}")
    bot_token = "Bearer " + values["CODEBOT_SLACK_BOT_TOKEN"]
    app_token = "Bearer " + values["CODEBOT_SLACK_APP_TOKEN"]
    auth = _get_json("https://slack.com/api/auth.test", bot_token, method="POST",
                     include_scopes=True)
    if not isinstance(auth, dict) or not auth.get("ok"):
        raise RuntimeError(f"Slack bot token (xoxb-) is invalid or revoked "
                           f"({auth.get('error', 'invalid') if isinstance(auth, dict) else 'invalid response'}); "
                           f"copy the Bot User OAuth Token from OAuth & Permissions at {SLACK_APP_SETTINGS_URL}")
    if auth.get("_granted_scopes"):
        granted = {scope.strip() for scope in auth["_granted_scopes"].split(",")}
        required = {"channels:read", "channels:history", "chat:write", "files:write"}
        if missing := required - granted:
            raise RuntimeError(f"Slack bot lacks {', '.join(sorted(missing))}. Add these Bot Token Scopes "
                               f"under OAuth & Permissions at {SLACK_APP_SETTINGS_URL}, reinstall "
                               "the app and retry")
    connection = _get_json("https://slack.com/api/apps.connections.open", app_token,
                           method="POST")
    if not isinstance(connection, dict) or not connection.get("ok"):
        raise RuntimeError(f"Slack Socket Mode app token (xapp-): "
                           f"{connection.get('error', 'invalid') if isinstance(connection, dict) else 'invalid response'}; "
                           f"enable Socket Mode and create an app-level token at {SLACK_APP_SETTINGS_URL}")
    found = {}
    cursor = ""
    seen_cursors = set()
    while True:
        query = {"types": "public_channel", "exclude_archived": "true", "limit": "200"}
        if cursor:
            query["cursor"] = cursor
        url = "https://slack.com/api/conversations.list?" + urllib.parse.urlencode(query)
        page = _get_json(url, bot_token)
        if not isinstance(page, dict) or not page.get("ok"):
            reason = page.get("error", "invalid response") if isinstance(page, dict) else "invalid response"
            if reason == "missing_scope":
                needed = page.get("needed") or "channels:read"
                raise RuntimeError(f"Slack bot lacks {needed}; add that Bot Token Scope under "
                                   f"OAuth & Permissions at {SLACK_APP_SETTINGS_URL} and reinstall the app")
            raise RuntimeError(f"Slack cannot list channels ({reason}); check the bot token, "
                               "workspace and channel access, then retry")
        for channel in page.get("channels", []):
            if (isinstance(channel, dict) and channel.get("is_channel") and
                    not channel.get("is_private") and not channel.get("is_archived") and
                    channel.get("is_member") and channel.get("id", "").startswith("C") and
                    channel.get("name")):
                found[channel["id"]] = {"id": channel["id"], "name": channel["name"]}
        next_cursor = (page.get("response_metadata") or {}).get("next_cursor", "")
        if not next_cursor:
            break
        if next_cursor in seen_cursors:
            raise RuntimeError("Slack returned a repeated channel pagination cursor")
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    if not found:
        workspace = auth.get("team", "this workspace")
        raise RuntimeError(f"No joined public channels found in {workspace}; invite this app's "
                           f"bot to a public channel in Slack and retry ({SLACK_APP_SETTINGS_URL})")
    return sorted(found.values(), key=lambda item: item["name"].casefold())


def _github_project_owner(values: dict[str, str]) -> str:
    if url := values.get("CODEBOT_GH_PROJECT_URL"):
        if match := re.search(r"github\.com/(?:orgs|users)/([^/\s]+)/projects/\d+", url):
            return match.group(1)
    if owner := values.get("CODEBOT_GH_PROJECT_OWNER", "").strip():
        return owner
    repo = values.get("CODEBOT_REPO_PATH", "")
    if not repo:
        raise RuntimeError("Configure Repository > CODEBOT_REPO_PATH first to find the board owner")
    try:
        remote = subprocess.run(["git", "-C", repo, "remote", "get-url", "origin"],
                                capture_output=True, text=True, timeout=12)
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(f"Cannot read the target repo's origin: {error}") from error
    if remote.returncode or not (match := re.search(
            r"[:/]([^/:\s]+)/([^/\s]+?)(?:\.git)?/?$", remote.stdout.strip())):
        raise RuntimeError("Cannot determine GitHub owner from origin; enter "
                           "CODEBOT_GH_PROJECT_OWNER in Advanced or use a manual board URL")
    return match.group(1)


_GH_PROJECTS_QUERY = """query($owner: String!, $cursor: String) {
  repositoryOwner(login: $owner) {
    ... on ProjectV2Owner {
      projectsV2(first: 100, after: $cursor) {
        nodes { number title url }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}"""


def fetch_github_projects(values: dict[str, str]) -> list[dict[str, str]]:
    """Discover boards accessible to GH_TOKEN for the target repo/board owner."""
    token = values.get("GH_TOKEN", "").strip()
    if not token:
        raise RuntimeError(f"Enter GH_TOKEN in Repository first ({GITHUB_TOKEN_URL})")
    owner = _github_project_owner(values)
    boards = {}
    cursor = None
    while True:
        result = _get_json("https://api.github.com/graphql", "Bearer " + token,
                           method="POST", payload={"query": _GH_PROJECTS_QUERY,
                                                   "variables": {"owner": owner, "cursor": cursor}})
        if not isinstance(result, dict):
            raise RuntimeError("GitHub returned an invalid Projects response")
        if result.get("errors"):
            details = "; ".join(error.get("message", "unknown error")
                                for error in result["errors"])
            raise RuntimeError(f"Cannot list GitHub Projects for {owner}: {details}. "
                               "GH_TOKEN needs project write permission (and org SSO approval, if enabled)")
        project_data = ((result.get("data") or {}).get("repositoryOwner") or {})
        page = project_data.get("projectsV2") or {}
        if not project_data or not isinstance(page.get("nodes"), list):
            raise RuntimeError(f"Cannot list Projects for {owner}; check the owner and GH_TOKEN scopes")
        for node in page["nodes"]:
            if node and node.get("url") and node.get("title"):
                boards[node["url"]] = {"url": node["url"], "title": node["title"]}
        info = page.get("pageInfo") or {}
        if not info.get("hasNextPage"):
            break
        new_cursor = info.get("endCursor")
        if not new_cursor or new_cursor == cursor:
            raise RuntimeError("GitHub repeated or omitted its project pagination cursor")
        cursor = new_cursor
    if not boards:
        raise RuntimeError(f"No accessible Projects v2 boards for {owner}. Check "
                           "GH_TOKEN project scope or choose a manual URL for another owner")
    return sorted(boards.values(), key=lambda board: (board["title"].casefold(), board["url"]))


def _slack_channel_error(values: dict[str, str]) -> str:
    """Explain why the selected channel cannot be used, without exposing tokens."""
    channel_id = values["CODEBOT_SLACK_CHANNEL_ID"]
    url = ("https://slack.com/api/conversations.info?" +
           urllib.parse.urlencode({"channel": channel_id}))
    response = _get_json(url, "Bearer " + values["CODEBOT_SLACK_BOT_TOKEN"])
    if not isinstance(response, dict):
        return "Slack returned an invalid channel response; retry channel discovery"
    if not response.get("ok"):
        reason = response.get("error", "unknown error")
        if reason in ("invalid_auth", "not_authed", "token_revoked"):
            return ("Slack bot token (xoxb-) is invalid or revoked; copy a new Bot User OAuth "
                    f"Token from OAuth & Permissions at {SLACK_APP_SETTINGS_URL}")
        if reason == "missing_scope":
            needed = response.get("needed") or "channels:read"
            return (f"Slack bot token lacks {needed}. Add that bot scope under OAuth & Permissions "
                    f"at {SLACK_APP_SETTINGS_URL}, reinstall the app, then retry")
        if reason in ("channel_not_found", "not_in_channel"):
            return (f"The bot cannot access channel {channel_id} ({reason}). Verify the xoxb token "
                    "belongs to this app, invite that bot through the channel's Add people/apps menu, "
                    "then reload joined channels")
        return f"Slack could not inspect channel {channel_id}: {reason}; check the bot token and channel access"
    channel = response.get("channel") or {}
    if not channel.get("is_channel") or channel.get("is_private"):
        return f"Channel {channel_id} is not a public channel; select a public channel"
    if channel.get("is_archived"):
        return f"Channel {channel_id} is archived; select an active public channel"
    if not channel.get("is_member"):
        name = channel.get("name", channel_id)
        return (f"The bot is not a member of #{name} ({channel_id}). Open that public Slack "
                "channel, choose Add people/apps, invite this instance's app, then reload channels. "
                "The selected xoxb token must belong to the app you invited")
    return f"Channel {channel_id} is not in the bot's joined-channel list; reload channels and retry"


def _slack_history_error(values: dict[str, str]) -> str | None:
    """Probe read access; discovery and membership alone cannot prove history scope."""
    channel_id = values["CODEBOT_SLACK_CHANNEL_ID"]
    url = "https://slack.com/api/conversations.history?" + urllib.parse.urlencode(
        {"channel": channel_id, "limit": 1})
    response = _get_json(url, "Bearer " + values["CODEBOT_SLACK_BOT_TOKEN"])
    if isinstance(response, dict) and response.get("ok"):
        return None
    reason = response.get("error", "invalid response") if isinstance(response, dict) else "invalid response"
    if reason == "missing_scope":
        needed = response.get("needed") or "channels:history"
        return (f"Slack bot lacks {needed}. In {SLACK_APP_SETTINGS_URL} open this app's "
                "OAuth & Permissions, add that Bot Token Scope and reinstall the app; "
                "then reload channels and Test again")
    if reason in ("not_in_channel", "channel_not_found"):
        return _slack_channel_error(values)
    if reason in ("invalid_auth", "not_authed", "token_revoked"):
        return ("Slack bot token is invalid or revoked. Copy the Bot User OAuth Token (xoxb-) "
                f"from OAuth & Permissions at {SLACK_APP_SETTINGS_URL} and retry")
    return f"Slack cannot read channel {channel_id}: {reason}; check the bot's access and retry"


def preflight(values: dict[str, str]) -> list[str]:
    """Check selected Jira/Slack credentials before replacing a working .env."""
    errors = []
    if (values.get("CODEBOT_TASK_SOURCE") or "gdoc") == "jira":
        try:
            available = {name.casefold() for name in fetch_jira_statuses(values)}
            for setting in JIRA_STATUS_KEYS:
                if (values.get(setting) or BY_KEY[setting].default).casefold() not in available:
                    errors.append(f"{setting}: unavailable in project {values['CODEBOT_JIRA_PROJECT_KEY']}")
        except RuntimeError as error:
            errors.append(f"Jira credentials/project: {error}")

    if (values.get("CODEBOT_TASK_SOURCE") or "gdoc") == "github":
        try:
            boards = fetch_github_projects(values)
            selected_url = values.get("CODEBOT_GH_PROJECT_URL", "").rstrip("/")
            if selected_url:
                available = {board["url"].rstrip("/") for board in boards}
                if selected_url not in available:
                    raise RuntimeError("Selected board is not accessible to GH_TOKEN for this owner; "
                                       "check the URL, project scope and org SSO authorization")
            elif number := values.get("CODEBOT_GH_PROJECT_NUMBER"):
                if not any(board["url"].rstrip("/").endswith(f"/projects/{number}")
                           for board in boards):
                    raise RuntimeError(f"Project #{number} is not accessible to GH_TOKEN")
        except RuntimeError as error:
            errors.append(f"GitHub Projects: {error}")

    if (values.get("CODEBOT_COMM_CHANNEL") or "email") == "slack":
        try:
            channels = fetch_slack_channels(values)
            if values.get("CODEBOT_SLACK_CHANNEL_ID") not in {item["id"] for item in channels}:
                raise RuntimeError(_slack_channel_error(values))
            if problem := _slack_history_error(values):
                raise RuntimeError(problem)
        except RuntimeError as error:
            errors.append(f"Slack channel/app: {error}")
    return errors


def _google_access_errors(values: dict[str, str], scopes: list[str]) -> list[str]:
    if not scopes:
        return []
    path = Path(values.get("CODEBOT_DATA_DIR") or ROOT / "data") / "token.json"
    try:
        granted = set(json.loads(path.read_text()).get("scopes") or [])
    except (OSError, ValueError):
        granted = set()
    if not set(scopes).issubset(granted):
        return [f"Google OAuth token at {path} is missing or lacks required scopes. "
                "Create data/credentials.json and run python3 scripts/setup_oauth.py "
                "(or choose Authorize Google in setup), then test again"]
    return []


def authorize_google(values: dict[str, str]) -> None:
    """Run consent with unsaved section choices passed through the process env."""
    if not _google_scopes(values):
        raise RuntimeError("The selected settings do not require Google OAuth")
    data_dir = Path(values.get("CODEBOT_DATA_DIR") or ROOT / "data")
    credentials = data_dir / "credentials.json"
    if not credentials.is_file():
        raise RuntimeError(f"Download a Google Desktop OAuth client JSON from "
                           f"https://console.cloud.google.com/apis/credentials to {credentials}, "
                           "then try Authorize Google again")
    python = ROOT / ".venv/bin/python"
    try:
        if not python.exists():
            subprocess.run([sys.executable, "-m", "venv", str(ROOT / ".venv")],
                           check=True, timeout=60)
        subprocess.run([str(python), "-m", "pip", "install", "-r",
                        str(ROOT / "requirements.txt"), "--index-url", "https://pypi.org/simple",
                        "--timeout", "10", "--retries", "1"],
                       check=True, timeout=120, env=os.environ | {"PIP_CONFIG_FILE": os.devnull})
        subprocess.run([str(python), "scripts/setup_oauth.py"], cwd=ROOT,
                       env=os.environ | values, check=True, timeout=180)
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(f"Google OAuth did not complete: {error}. "
                           "Check credentials and retry") from error


def opencode_authenticated(values: dict[str, str]) -> bool:
    """Probe the selected provider's persisted container login."""
    provider = values.get("OPENCODE_PROVIDER", "")
    if not provider:
        return False
    command = ["docker", "compose", "run", "--rm", "--no-deps", "--entrypoint", "opencode",
               "-e", "XDG_DATA_HOME=/app/data/opencode/data", "-e",
               "XDG_CONFIG_HOME=/app/data/opencode/config", "-e",
               "XDG_CACHE_HOME=/app/data/opencode/cache", "-e",
               "XDG_STATE_HOME=/app/data/opencode/state", "codebot", "auth", "list"]
    try:
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=45)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and provider.casefold() in result.stdout.casefold()


def authenticate_opencode(values: dict[str, str]) -> None:
    """Authenticate a provider before saving the Agent section."""
    provider = values.get("OPENCODE_PROVIDER", "").strip()
    if not provider:
        raise RuntimeError("Enter OPENCODE_PROVIDER before authenticating")
    if not values.get("CODEBOT_REPO_PATH") or not values.get("GH_TOKEN"):
        raise RuntimeError("Save the Repository section before OpenCode login")
    command = ["docker", "compose", "run", "--rm", "--no-deps", "--entrypoint", "opencode",
               "-e", "XDG_DATA_HOME=/app/data/opencode/data", "-e",
               "XDG_CONFIG_HOME=/app/data/opencode/config", "-e",
               "XDG_CACHE_HOME=/app/data/opencode/cache", "-e",
               "XDG_STATE_HOME=/app/data/opencode/state", "codebot", "auth", "login",
               "--provider", provider]
    try:
        subprocess.run(["docker", "compose", "build", "codebot"], cwd=ROOT, check=True,
                       timeout=1200)
        if "azure" in provider.casefold():
            print("Azure Resource Name: open your Azure OpenAI resource in the Azure portal "
                  "(Overview or Keys and Endpoint). In an endpoint such as "
                  "https://my-team-ai.openai.azure.com/, enter my-team-ai — not the full URL "
                  "or your model deployment name.", flush=True)
        subprocess.run(command, cwd=ROOT, check=True, timeout=600)
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(f"OpenCode login failed for {provider}: {error}. "
                           "Check Docker and retry") from error


def test_section(section: str, values: dict[str, str],
                 changed_keys: set[str] | None = None) -> list[str]:
    """Test one independently saved section, without requiring future sections."""
    errors = []
    settings = fields_for(section, values)
    for setting in settings:
        if section == "Advanced" and setting.key not in (changed_keys or set()):
            continue
        if problem := validate_value(setting, effective(setting, values)):
            errors.append(f"{setting.key}: {problem}")

    def require(*keys: str) -> None:
        for key in keys:
            if not (values.get(key) or BY_KEY[key].default):
                errors.append(f"{key}: required in {section}; open that section to set it")

    if section == "Repository":
        require("CODEBOT_REPO_PATH", "GH_TOKEN")
        if not errors:
            try:
                account = _get_json("https://api.github.com/user", "Bearer " + values["GH_TOKEN"])
                if not isinstance(account, dict) or not account.get("login"):
                    raise RuntimeError("GitHub did not recognize this token")
            except RuntimeError as error:
                errors.append(f"GH_TOKEN: {error}; create a token at {GITHUB_TOKEN_URL}")
    elif section == "Backlog":
        source = values.get("CODEBOT_TASK_SOURCE") or "gdoc"
        if source == "gdoc":
            require("CODEBOT_DOC_ID")
            errors += _google_access_errors(values, ["https://www.googleapis.com/auth/documents",
                                                     "https://www.googleapis.com/auth/drive"])
        elif source == "jira":
            require("CODEBOT_JIRA_URL", "CODEBOT_JIRA_EMAIL", "CODEBOT_JIRA_API_TOKEN",
                    "CODEBOT_JIRA_PROJECT_KEY", "CODEBOT_JIRA_PICK_LABEL")
            if not errors:
                try:
                    projects = fetch_jira_projects(values)
                    if values["CODEBOT_JIRA_PROJECT_KEY"] not in {p["key"] for p in projects}:
                        raise RuntimeError(f"Project {values['CODEBOT_JIRA_PROJECT_KEY']} is not visible "
                                           "to this Jira account; reload accessible projects")
                    available = {name.casefold() for name in fetch_jira_statuses(values)}
                    for key in JIRA_STATUS_KEYS:
                        if (values.get(key) or BY_KEY[key].default).casefold() not in available:
                            errors.append(f"{key}: not a workflow status in this project; "
                                          "reload Jira statuses and choose from the dropdown")
                except RuntimeError as error:
                    errors.append(f"Jira: {error}")
        elif source == "github":
            if not values.get("CODEBOT_GH_PROJECT_URL") and not (
                    values.get("CODEBOT_GH_PROJECT_OWNER") and values.get("CODEBOT_GH_PROJECT_NUMBER")):
                errors.append("CODEBOT_GH_PROJECT_URL: select a board or enter a manual URL")
            if not values.get("GH_TOKEN"):
                errors.append("GH_TOKEN: configure Repository before testing GitHub Projects")
            if not errors:
                errors.extend(preflight(values | {"CODEBOT_COMM_CHANNEL": "email"}))
    elif section == "Conversation":
        channel = values.get("CODEBOT_COMM_CHANNEL") or "email"
        if channel == "email":
            require("CODEBOT_USER_EMAIL")
            errors += _google_access_errors(values, ["https://www.googleapis.com/auth/gmail.modify"])
        else:
            require("CODEBOT_SLACK_BOT_TOKEN", "CODEBOT_SLACK_APP_TOKEN", "CODEBOT_SLACK_CHANNEL_ID")
            if not errors:
                errors.extend(preflight(values | {"CODEBOT_TASK_SOURCE": "gdoc"}))
    elif section == "Agent":
        if (values.get("CODEBOT_AGENT") or "claude") == "opencode":
            require("OPENCODE_PROVIDER", "OPENCODE_MODEL")
            model = values.get("OPENCODE_MODEL", "")
            provider = values.get("OPENCODE_PROVIDER", "")
            if model and provider and not model.startswith(provider + "/"):
                errors.append("OPENCODE_MODEL: choose a model from the authenticated "
                              "provider using `opencode models <provider>`")
            if not errors and not opencode_authenticated(values):
                errors.append("OpenCode provider is not authenticated in data/opencode; "
                              "select Authenticate OpenCode in Agent and retry")
        else:
            require("CLAUDE_CODE_OAUTH_TOKEN")
    elif section == "Evidence":
        if (values.get("CODEBOT_EVIDENCE_UPLOAD") or "on").lower() not in ("off", "false", "0", "no"):
            errors += _google_access_errors(values, ["https://www.googleapis.com/auth/drive"])
    return errors


def test_all(values: dict[str, str]) -> list[str]:
    """The main menu's full test checks every selected integration at once."""
    errors = validate(values)
    for section in PRIMARY_PAGES:
        errors.extend(test_section(section, values))
    return list(dict.fromkeys(errors))


def _apply(env: EnvFile, changes: dict[str, str]) -> bool:
    errors = validate(env.with_changes(changes))
    if errors:
        print("Fix these settings before saving:\n- " + "\n- ".join(errors))
        return False
    if errors := preflight(env.with_changes(changes)):
        print("Check integration access before saving:\n- " + "\n- ".join(errors))
        return False
    print("\nReview changes (secrets masked):\n" + env.preview(changes))
    if input("Save configuration? [y/N] ").strip().lower() not in ("y", "yes"):
        print("Canceled; .env was not changed.")
        return False
    backup = env.save(changes)
    print(f"Saved {env.path}" + (f" (backup: {backup})" if backup else ""))
    _post_save_auth(env.with_changes(changes))
    return True


def _text_section(env: EnvFile, page: str) -> EnvFile:
    """Edit one section's draft; only a successful Save and Close writes to disk."""
    changes = {}
    values = dict(env.values)
    while True:
      for page in (() if page == "Advanced" else (page,)):
        print(f"\n━━ {page} ━━")
        queue = list(fields_for(page, values))
        index = 0
        jira_projects = None
        jira_statuses = None
        slack_channels = None
        github_boards = None
        while index < len(queue):
            setting = queue[index]
            current = effective(setting, values)
            if setting.key == "CODEBOT_SELF_HEAL":
                print(SELF_HEAL_SETUP_HELP)
                for number, (label, mode) in enumerate(SELF_HEAL_OPTIONS, 1):
                    print(f"  {number}. {label} ({mode})")
                try:
                    entered = input(f"CODEBOT_SELF_HEAL [{current or 'off'}] "
                                    "(number or mode; Enter keeps; /clear disables): ").strip().lower()
                except EOFError:
                    print("Draft discarded; .env was not changed.")
                    return env
                if entered.isdecimal() and 1 <= int(entered) <= len(SELF_HEAL_OPTIONS):
                    value = SELF_HEAL_OPTIONS[int(entered) - 1][1]
                else:
                    value = "off" if entered == "/clear" else entered or current or "off"
                if error := validate_value(setting, value):
                    print(error)
                    continue
                values[setting.key] = value
                if env.values.get(setting.key) != value:
                    changes[setting.key] = value
                index += 1
                continue
            if setting.key == "CODEBOT_GH_PROJECT_URL":
                if github_boards is None:
                    try:
                        github_boards = fetch_github_projects(values)
                    except RuntimeError as error:
                        print(f"Unable to list GitHub Projects: {error}")
                        try:
                            action = input("Retry, enter another GH_TOKEN, use a manual URL, or cancel? "
                                           "[r/e/m/q] ").strip().lower()
                        except EOFError:
                            return env
                        if action == "e":
                            print("Open Repository from the main menu, save GH_TOKEN, then reopen Backlog.")
                            return env
                        elif action == "m":
                            github_boards = []
                        elif action == "q":
                            print("Setup canceled; no configuration written.")
                            return env
                        continue
                if github_boards:
                    print("Choose an accessible GitHub Projects v2 board:")
                    for number, board in enumerate(github_boards, 1):
                        print(f"  {number}. {board['title']} ({board['url']})")
                    selected = next((board for board in github_boards
                                     if board["url"].rstrip("/") == current.rstrip("/")), None)
                    try:
                        entry = input(f"CODEBOT_GH_PROJECT_URL [current: "
                                      f"{selected['title'] if selected else 'none'}] "
                                      "(number or m for a manual URL): ").strip().lower()
                    except EOFError:
                        return env
                    if not entry and selected:
                        value = selected["url"]
                    elif entry.isdecimal() and 1 <= int(entry) <= len(github_boards):
                        value = github_boards[int(entry) - 1]["url"]
                    elif entry == "m":
                        github_boards = []
                        continue
                    else:
                        print("Choose a listed board or m for a different owner.")
                        continue
                else:
                    print("Manual board URL for another owner. It will be checked using GH_TOKEN before saving.")
                    try:
                        value = input(f"CODEBOT_GH_PROJECT_URL [{current or 'none'}]: ").strip() or current
                    except EOFError:
                        return env
                    if error := validate_value(setting, value):
                        print(error)
                        continue
                    if not value:
                        print("Enter a board URL.")
                        continue
                values[setting.key] = value
                if env.values.get(setting.key) != value:
                    changes[setting.key] = value
                index += 1
                continue
            if setting.key == "CODEBOT_JIRA_PROJECT_KEY":
                if jira_projects is None:
                    try:
                        jira_projects = fetch_jira_projects(values)
                    except RuntimeError as error:
                        print(f"Unable to load Jira projects: {error}")
                        try:
                            action = input("Edit Jira connection, retry or cancel? [e/r/q] ").strip().lower()
                        except EOFError:
                            return env
                        if action == "e":
                            index = next(i for i, field in enumerate(queue)
                                         if field.key == "CODEBOT_JIRA_URL")
                        elif action == "q":
                            print("Setup canceled; no configuration written.")
                            return env
                        continue
                print("Choose a Jira project visible to this account:")
                for number, project in enumerate(jira_projects, 1):
                    print(f"  {number}. {project['name']} ({project['key']})")
                selected = next((project for project in jira_projects
                                 if project["key"] == current), None)
                try:
                    entry = input(f"CODEBOT_JIRA_PROJECT_KEY [current: "
                                  f"{selected['name'] if selected else 'none'}] "
                                  "(number, project name or key; Enter keeps current): ").strip()
                except EOFError:
                    print("Setup canceled; no configuration written.")
                    return env
                if not entry and selected:
                    chosen = selected
                elif entry.isdecimal() and 1 <= int(entry) <= len(jira_projects):
                    chosen = jira_projects[int(entry) - 1]
                else:
                    chosen = next((project for project in jira_projects
                                   if entry.casefold() in (project["key"].casefold(),
                                                           project["name"].casefold())), None)
                if chosen is None:
                    print("Choose an accessible Jira project from the list above.")
                    continue
                if chosen["key"] != current:
                    jira_statuses = None
                values[setting.key] = chosen["key"]
                if env.values.get(setting.key) != chosen["key"]:
                    changes[setting.key] = chosen["key"]
                index += 1
                continue
            if setting.key == "CODEBOT_SLACK_CHANNEL_ID":
                if slack_channels is None:
                    try:
                        slack_channels = fetch_slack_channels(values)
                    except RuntimeError as error:
                        print(f"Unable to load Slack channels: {error}")
                        try:
                            action = input("Edit Slack tokens, retry or cancel? [e/r/q] ").strip().lower()
                        except EOFError:
                            return env
                        if action == "e":
                            index = next(i for i, field in enumerate(queue)
                                         if field.key == "CODEBOT_SLACK_BOT_TOKEN")
                        elif action == "q":
                            print("Setup canceled; no configuration written.")
                            return env
                        continue
                print("Choose a public Slack channel this bot has joined:")
                for number, channel in enumerate(slack_channels, 1):
                    print(f"  {number}. #{channel['name']} ({channel['id']})")
                current_channel = next((channel for channel in slack_channels
                                        if channel["id"] == current), None)
                try:
                    entry = input(f"CODEBOT_SLACK_CHANNEL_ID [current: "
                                  f"{('#' + current_channel['name']) if current_channel else 'none'}] "
                                  "(number, channel name or ID; Enter keeps current): ").strip()
                except EOFError:
                    print("Setup canceled; no configuration written.")
                    return env
                if not entry and current_channel:
                    chosen = current_channel
                elif entry.isdecimal() and 1 <= int(entry) <= len(slack_channels):
                    chosen = slack_channels[int(entry) - 1]
                else:
                    chosen = next((channel for channel in slack_channels
                                   if entry.casefold().lstrip("#") == channel["name"].casefold()
                                   or entry == channel["id"]), None)
                if chosen is None:
                    print("Choose a joined public channel from the list above.")
                    continue
                values[setting.key] = chosen["id"]
                if env.values.get(setting.key) != chosen["id"]:
                    changes[setting.key] = chosen["id"]
                index += 1
                continue
            if setting.key in JIRA_STATUS_KEYS:
                if jira_statuses is None:
                    try:
                        jira_statuses = fetch_jira_statuses(values)
                    except RuntimeError as error:
                        print(f"Unable to load statuses: {error}")
                        try:
                            action = input("Edit Jira connection, retry or cancel? [e/r/q] ").strip().lower()
                        except EOFError:
                            return env
                        if action == "e":
                            jira_projects = None
                            jira_statuses = None
                            index = next(i for i, field in enumerate(queue)
                                         if field.key == "CODEBOT_JIRA_URL")
                        elif action == "q":
                            print("Setup canceled; no configuration written.")
                            return env
                        continue
                print(f"{setting.key}: {setting.help}")
                for number, status in enumerate(jira_statuses, 1):
                    print(f"  {number}. {status}")
                selected = next((name for name in jira_statuses
                                 if name.casefold() == current.casefold()), None)
                try:
                    entry = input(f"Select a status [current: {selected or 'none'}] "
                                  "(number or name; Enter keeps current): ").strip()
                except EOFError:
                    print("Setup canceled; no configuration written.")
                    return env
                if not entry and selected:
                    value = selected
                elif entry.isdecimal() and 1 <= int(entry) <= len(jira_statuses):
                    value = jira_statuses[int(entry) - 1]
                else:
                    value = next((name for name in jira_statuses
                                  if name.casefold() == entry.casefold()), None)
                if value is None:
                    print("Choose one of the Jira statuses listed above.")
                    continue
                values[setting.key] = value
                if env.values.get(setting.key) != value:
                    changes[setting.key] = value
                index += 1
                continue
            shown = "(set; hidden)" if setting.secret and current else current or "(empty)"
            choices = " / ".join(choice or "default (blank)" for choice in setting.choices)
            print(setting.help + (f" Options: {choices}" if choices else ""))
            prompt = f"{setting.key} [{shown}] (Enter keeps; /clear empties): "
            try:
                entered = (getpass.getpass(prompt) if setting.secret and sys.stdin.isatty()
                           else input(prompt)).strip()
            except EOFError:
                print("Setup canceled; no configuration written.")
                return env
            value = "" if entered == "/clear" else current if not entered else entered
            if setting.key == "CODEBOT_JIRA_PICK_LABEL" and not value:
                print("  Enter the opt-in label Jira issues must have to become tasks.")
                continue
            if error := validate_value(setting, value):
                print(f"  {error}")
                continue
            if entered:
                value = normalize_value(setting, value)
                if setting.key in ("CODEBOT_JIRA_URL", "CODEBOT_JIRA_EMAIL",
                                   "CODEBOT_JIRA_API_TOKEN") and value != current:
                    jira_projects = None
                    jira_statuses = None
                if setting.key in ("CODEBOT_SLACK_BOT_TOKEN", "CODEBOT_SLACK_APP_TOKEN") and value != current:
                    slack_channels = None
                values[setting.key] = value
                changes[setting.key] = value
            if setting.key in ("CODEBOT_TASK_SOURCE", "CODEBOT_COMM_CHANNEL", "CODEBOT_AGENT"):
                queue = list(fields_for(page, values))
            index += 1
      if page == "Advanced":
        while True:
            try:
                search = input("Search settings (blank to finish): ").strip()
            except EOFError:
                return env
            if not search:
                break
            matching = fields_for("Advanced", values, search)
            for setting in matching:
                print(f"  {setting.key}: {setting.help}")
            try:
                key = input("Enter a key to edit (blank to search again): ").strip().upper()
            except EOFError:
                return env
            if key not in BY_KEY:
                continue
            setting = BY_KEY[key]
            shown = "(set; hidden)" if setting.secret and effective(setting, values) else effective(setting, values) or "(empty)"
            try:
                entered = (getpass.getpass(f"{key} [{shown}]: ") if setting.secret and sys.stdin.isatty()
                           else input(f"{key} [{shown}]: ")).strip()
            except EOFError:
                return env
            if not entered:
                continue
            value = "" if entered == "/clear" else entered
            if error := validate_value(setting, value):
                print(error)
                continue
            value = normalize_value(setting, value)
            values[key] = value
            changes[key] = value
      print("\nPending changes (secrets masked):\n" + env.preview(changes))
      try:
          action = input(f"{page}: [t] Test, [s] Save and Close, [e] Edit, [d] Discard: ").strip().lower()
      except EOFError:
          print("Draft discarded; .env was not changed.")
          return env
      if action == "d":
          return env
      if action not in ("t", "s", "e"):
          print("Choose t, s, e or d.")
          continue
      if action == "e":
          continue
      errors = test_section(page, values, set(changes))
      if errors:
          print(f"{page} failed; nothing was saved:\n- " + "\n- ".join(errors))
          continue
      if action == "t":
          print(f"{page} passed. Save and Close to persist this section.")
          continue
      try:
          backup = env.save(changes, validator=lambda _values: [])
      except (ValueError, OSError) as error:
          print(f"{page} could not be saved: {error}")
          continue
      print(f"{page} passed and saved to {env.path}" + (f" (backup: {backup})" if backup else ""))
      return EnvFile(env.path)


def text_setup(env: EnvFile) -> bool:
    """Menu-driven stdlib fallback with immediate, independent section saves."""
    pages = (*PRIMARY_PAGES, "Advanced")
    completed: set[str] = set()
    while True:
        print("\n━━ Coderbot configuration ━━")
        for number, page in enumerate(pages, 1):
            print(f"  {number}. [{'x' if page in completed else ' '}] {page}")
        try:
            selection = input("Select a section, [t] Test full setup, [v] Sync to VM, [q] Exit: ").strip().lower()
        except EOFError:
            return True  # no draft exists on the main menu
        if selection in ("q", "exit"):
            return True
        if selection in ("t", "test"):
            errors = test_all(dict(env.values))
            print("Full test failed:\n- " + "\n- ".join(errors) if errors else
                  "Full test passed: the saved configuration is ready.")
            continue
        if selection in ("v", "vm"):
            try:
                host = input("SSH host (user@host): ").strip()
                mode = input("[u] Update code (default), [i] Initial copy: ").strip().lower()
                if mode not in ("", "u", "i"):
                    print("Choose u or i; nothing copied.")
                    continue
                if mode == "i" and not _confirm(
                        "Bots stopped on both machines? Initial copy includes configuration, "
                        "state and the target checkout. Continue?"):
                    print("Canceled; nothing copied.")
                    continue
                restart = _confirm("Recreate the VM container and check its health after copying?")
            except EOFError:
                print("Canceled; nothing copied.")
                continue
            try:
                print("Synchronizing with the VM; output follows...")
                print(sync_to_vm(host, code_only=mode != "i", restart=restart))
            except (ValueError, RuntimeError) as error:
                print(error)
            continue
        if not selection.isdecimal() or not 1 <= int(selection) <= len(pages):
            print("Choose a section number, t, v or q.")
            continue
        section = pages[int(selection) - 1]
        updated = _text_section(env, section)
        if updated is not env:
            if updated.values != env.values:
                dependents = {
                    "Repository": {"Backlog", "Conversation", "Agent", "Evidence"},
                    "Backlog": {"Conversation", "Evidence"},
                    "Conversation": {"Backlog", "Evidence"},
                    "Evidence": {"Backlog", "Conversation"},
                    "Agent": set(),
                    "Self-healing": set(),
                    "Advanced": set(PRIMARY_PAGES),
                }
                completed.difference_update(dependents[section])
            completed.add(section)
            env = updated


def _ensure_textual():
    host_venv = ROOT / ".venv"
    python = host_venv / "bin/python"
    if not python.exists():
        subprocess.run([sys.executable, "-m", "venv", str(host_venv)], check=True, timeout=60)
    elif subprocess.run([str(python), "-c", "import textual"], capture_output=True,
                        timeout=10).returncode == 0:
        return python
    # All wheels are universal Python packages; fall back to pinned requirements
    # through the public index if any direct URL becomes unavailable.
    try:
        install = subprocess.run([str(python), "-m", "pip", "install", "--no-index",
                                  "--timeout", "10", "--retries", "1", *_SETUP_WHEELS],
                                 timeout=100, capture_output=True, text=True)
    except subprocess.TimeoutExpired:
        install = None
    if install is None or install.returncode:
        install = subprocess.run([str(python), "-m", "pip", "install", "-r",
                                  str(ROOT / "scripts/requirements-setup.txt"),
                                  "--index-url", "https://pypi.org/simple", "--timeout", "10",
                                  "--retries", "1"], timeout=100, capture_output=True, text=True,
                                 env=os.environ | {"PIP_CONFIG_FILE": os.devnull})
    if install.returncode:
        raise RuntimeError("Could not install Textual; check PyPI access or use --text")
    return python


def main():
    parser = argparse.ArgumentParser(description="Configure Coderbot (Jira, Slack, Google and GitHub)")
    parser.add_argument("--text", action="store_true", help="use the line-oriented fallback")
    args = parser.parse_args()
    env = EnvFile(ROOT / ".env")
    if not args.text and sys.stdin.isatty() and sys.stdout.isatty() and os.environ.get("TERM") not in ("dumb", ""):
        try:
            python = _ensure_textual()
            # Venv's bin/python is commonly a symlink to the system binary.
            # Comparing resolved executable paths mistakes the system interpreter
            # for the venv and then `import textual` fails despite a successful
            # install into .venv. sys.prefix identifies the active environment.
            if Path(sys.prefix).absolute() != (ROOT / ".venv").absolute():
                return subprocess.call([str(python), "-m", "scripts.setup"], cwd=ROOT,
                                       env=os.environ | {"CODEBOT_SETUP_TUI_READY": "1"})
            from scripts.setup_ui import run
            return 0 if run(env) else 1
        except (OSError, subprocess.SubprocessError, RuntimeError, ImportError) as error:
            print(f"Terminal UI unavailable ({error}); continuing in text mode.")
    if os.environ.get("CODEBOT_SETUP_TUI_READY") and not args.text:
        from scripts.setup_ui import run
        return 0 if run(env) else 1
    return 0 if text_setup(env) else 1


if __name__ == "__main__":
    raise SystemExit(main())
