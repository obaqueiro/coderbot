"""Single source of editable host setup settings and their validation metadata.

The audit test compares this catalog against every runtime environment read;
settings that cannot be edited by the installer must be explicitly classified.
"""
from dataclasses import dataclass
from pathlib import Path
import re

JIRA_API_TOKEN_URL = "https://id.atlassian.com/manage-profile/security/api-tokens"
JIRA_STATUS_KEYS = ("CODEBOT_JIRA_PICK_STATUS", "CODEBOT_JIRA_ACTIVE_STATUS",
                    "CODEBOT_JIRA_REVIEW_STATUS", "CODEBOT_JIRA_DONE_STATUS")
SLACK_APP_SETTINGS_URL = "https://api.slack.com/apps"
GITHUB_TOKEN_URL = "https://github.com/settings/tokens"
SELF_HEAL_OPTIONS = (
    ("Disabled", "off"),
    ("Observe only (no repairs)", "observe"),
    ("Propose repairs (manual promotion)", "propose"),
    ("Enabled (automatic validated repairs)", "auto"),
)
SELF_HEAL_SETUP_HELP = (
    "Automatic mode may edit Coderbot Python source and restart its worker. "
    "Start with Observe, then Propose before enabling automatic repairs. "
    "Rebuild the image and recreate the container to apply a mode change; "
    "saving setup only updates .env and never restarts the bot."
)


@dataclass(frozen=True)
class Setting:
    key: str
    group: str
    help: str
    default: str = ""
    kind: str = "text"
    secret: bool = False
    advanced: bool = False
    choices: tuple[str, ...] = ()


def field(key, group, help, default="", kind="text", secret=False, advanced=False, choices=()):
    return Setting(key, group, help, default, kind, secret, advanced, tuple(choices))


SETTINGS = (
    field("CODEBOT_REPO_PATH", "Repository", "Absolute path to a Git checkout or feature workspace.", kind="directory"),
    field("CODEBOT_WORKSPACE_MANIFEST", "Repository", "Optional JSON manifest inside the workspace for discovery, base overrides and checks.", advanced=True),
    field("CODEBOT_SELF_HEAL", "Self-healing", "Enable or disable controller self-healing.",
          "off", "choice", choices=tuple(value for _label, value in SELF_HEAL_OPTIONS)),
    field("CODEBOT_BASE_BRANCH", "Repository", "Trunk branch for task branches and PRs.", "main"),
    field("CODEBOT_PROJECT_NAME", "Repository", "Human-readable project name used in prompts."),
    field("CODEBOT_INSTANCE", "Repository", "Optional instance name; blank generates a stable adjective-animal name."),
    field("GIT_AUTHOR_NAME", "Repository", "Name used for Git commits.", "codebot"),
    field("GIT_AUTHOR_EMAIL", "Repository", "Email used for Git commits.", "codebot@localhost", "email"),
    field("GH_TOKEN", "Repository", f"GitHub token for PRs, git push and project issues. Create one at {GITHUB_TOKEN_URL}; Projects v2 also needs project write permission.", secret=True),
    field("CODEBOT_TASK_SOURCE", "Backlog", "Select the backlog provider.", "gdoc", "choice", choices=("gdoc", "github", "jira")),
    field("CODEBOT_DOC_ID", "GDoc", "Google Doc backlog ID (or paste the document URL)."),
    field("CODEBOT_DOC_SECTION", "GDoc", "Only use bullets under this heading."),
    field("CODEBOT_GH_PROJECT_URL", "GitHub", "Choose an accessible GitHub Projects v2 board by title; setup stores its URL. Choose Manual URL when the board belongs to another owner."),
    field("CODEBOT_GH_PROJECT_OWNER", "GitHub", "Board owner when URL is not supplied.", advanced=True),
    field("CODEBOT_GH_PROJECT_NUMBER", "GitHub", "Board number when URL is not supplied.", kind="positive", advanced=True),
    field("CODEBOT_GH_PROJECT_PICK_STATUSES", "GitHub", "Comma-separated selectable Status names.", "Ready", advanced=True),
    field("CODEBOT_GH_PROJECT_ACTIVE_STATUS", "GitHub", "Status during implementation.", "In progress", advanced=True),
    field("CODEBOT_GH_PROJECT_REVIEW_STATUS", "GitHub", "Status while a PR is open.", "In review", advanced=True),
    field("CODEBOT_GH_PROJECT_DONE_STATUS", "GitHub", "Status after merge or DONE.", "Done", advanced=True),
    field("CODEBOT_GH_LABEL_PREFIX", "GitHub", "Prefix for instance ownership labels.", "codebot", advanced=True),
    field("CODEBOT_GH_ISSUE_REPO", "GitHub", "Optional owner/repo for issues; defaults to origin.", advanced=True),
    field("CODEBOT_JIRA_URL", "Jira", "Jira Cloud site URL, https://site.atlassian.net."),
    field("CODEBOT_JIRA_EMAIL", "Jira", "Account email associated with the Jira API token.", kind="email"),
    field("CODEBOT_JIRA_API_TOKEN", "Jira", f"Create an API token for the Jira account above at {JIRA_API_TOKEN_URL} (Security > API tokens).", secret=True),
    field("CODEBOT_JIRA_PROJECT_KEY", "Jira", "Choose an accessible Jira project by name; setup stores its short key, e.g. ENG in ENG-42 (not the name or UUID)."),
    field("CODEBOT_JIRA_PICK_LABEL", "Jira", "Required opt-in label on issues Coderbot may pick, e.g. codebot-ready. Only issues with this label AND the eligible status are new tasks."),
    field("CODEBOT_JIRA_PICK_STATUS", "Jira", "Eligible issue workflow status (not board column).", "Ready"),
    field("CODEBOT_JIRA_ACTIVE_STATUS", "Jira", "Workflow status when claimed.", "In progress"),
    field("CODEBOT_JIRA_REVIEW_STATUS", "Jira", "Workflow status after PR creation.", "In review"),
    field("CODEBOT_JIRA_DONE_STATUS", "Jira", "Workflow status after merge or DONE.", "Done"),
    field("CODEBOT_JIRA_ISSUE_TYPE", "Jira", "Type for self-healing issues.", "Task", advanced=True),
    field("CODEBOT_COMM_CHANNEL", "Conversation", "Select email or Slack independently of backlog.", "email", "choice", choices=("email", "slack")),
    field("CODEBOT_USER_EMAIL", "Email", "First address receives email; comma-separated addresses may reply.", kind="email_list"),
    field("CODEBOT_SLACK_BOT_TOKEN", "Slack", f"Create a dedicated Slack app at {SLACK_APP_SETTINGS_URL}; in OAuth & Permissions add chat:write, channels:read, channels:history and files:write, install/reinstall it, then copy Bot User OAuth Token (xoxb-...).", secret=True),
    field("CODEBOT_SLACK_APP_TOKEN", "Slack", f"In the same app at {SLACK_APP_SETTINGS_URL}, enable Socket Mode and Event Subscriptions > Subscribe to bot events > message.channels; then Basic Information > App-Level Tokens > Generate Token and Scopes (connections:write); copy its xapp-... token.", secret=True),
    field("CODEBOT_SLACK_CHANNEL_ID", "Slack", "Select a public channel this bot has joined; setup fetches its name and stores the C... ID."),
    field("CODEBOT_AGENT", "Agent", "Coding agent CLI.", "claude", "choice", choices=("claude", "opencode")),
    field("USE_CAVEMAN", "Agent", "Usar Caveman", "false", "exact_bool"),
    field("CLAUDE_CODE_OAUTH_TOKEN", "Claude", "Headless Claude Code authentication token.", secret=True),
    field("CLAUDE_MODEL", "Claude", "Primary Claude model.", "claude-fable-5"),
    field("CLAUDE_EFFORT", "Claude", "Claude effort level.", "medium", "choice", choices=("low", "medium", "high")),
    field("CLAUDE_FALLBACK_MODEL", "Claude", "Fallback model; explicitly empty disables it.", "claude-opus-5", advanced=True),
    field("CLAUDE_FALLBACK_COOLDOWN_SECONDS", "Claude", "Delay before retrying primary model.", "3600", "nonnegative", advanced=True),
    field("CLAUDE_API_KEY", "Claude", "Optional key used by target repo apiKeyHelper.", secret=True, advanced=True),
    field("CLAUDE_CONFIG_PATH", "Claude", "Alternative Claude configuration location.", advanced=True),
    field("OPENCODE_PROVIDER", "OpenCode", "Provider ID used for OpenCode login."),
    field("OPENCODE_MODEL", "OpenCode", "Full provider/model identifier."),
    field("OPENCODE_EFFORT", "OpenCode", "Thinking/reasoning effort for GPT models. Low is faster; medium balances speed and depth; high/xhigh spend more time reasoning. Blank keeps OpenCode defaults. Supported levels depend on the model/provider.", kind="choice", choices=("", "none", "minimal", "low", "medium", "high", "xhigh")),
    field("CODEBOT_ACTIVITY_TRAIL", "Operations", "Post milestones to the backlog item.", "on", "switch", advanced=True),
    field("CODEBOT_SELF_HEAL_CODE_REVIEW", "Operations", "Seed missing review workflow tasks.", "on", "switch", advanced=True),
    field("CODEBOT_LOG_LEVEL", "Operations", "Runtime log verbosity.", "DEBUG", "choice", advanced=True, choices=("DEBUG", "INFO")),
    field("CODEBOT_ENVIRONMENT_NOTES", "Operations", "Project-specific environment notes.", advanced=True),
    field("CODEBOT_DATA_DIR", "Operations", "Persistent data directory; mount it in the container.", advanced=True),
    field("CODEBOT_POLL_INTERVAL", "Limits", "Seconds between idle backlog polls.", "120", "positive", advanced=True),
    field("CODEBOT_AGENT_TIMEOUT", "Limits", "Aggregate agent attempt timeout in seconds.", "1800", "positive", advanced=True),
    field("CODEBOT_CLAUDE_TIMEOUT", "Limits", "Legacy alias for agent timeout; retained on upgrades.", advanced=True),
    field("CODEBOT_E2E_TIMEOUT", "Limits", "E2E timeout in seconds.", "900", "positive", advanced=True),
    field("CODEBOT_UTILITY_TIMEOUT", "Limits", "Utility LLM timeout.", "30", "positive", advanced=True),
    field("CODEBOT_LOCAL_TOOL_TIMEOUT", "Limits", "Local tool operation and subagent queue timeout in seconds.", "200", "positive", advanced=True),
    field("CODEBOT_SUBAGENT_TIMEOUT", "Limits", "Subagent operation timeout.", "600", "positive", advanced=True),
    field("CODEBOT_PROVIDER_TIMEOUT", "Limits", "Provider retry timeout.", "180", "positive", advanced=True),
    field("CODEBOT_OPENCODE_TRANSPORT", "Limits", "OpenCode transport: http or legacy cli.", "http", advanced=True),
    field("CODEBOT_DETERMINISTIC_CHECKS", "Limits", "Use the deterministic final gate.", "on", advanced=True),
    field("CODEBOT_CONTEXT_TOKEN_BUDGET", "Limits", "Rotate sessions above context budget.", "80000", "positive", advanced=True),
    field("CODEBOT_OPENCODE_ROLE_MODELS", "Limits", "JSON role/model overrides.", "{}", advanced=True),
    field("CODEBOT_OPENCODE_ROLE_EFFORTS", "Limits", "JSON role/effort overrides.", "{}", advanced=True),
    field("CODEBOT_ARTIFACT_MANIFEST", "Operations", "Harness artifact manifest path.", advanced=True),
    field("CODEBOT_MAX_SUBAGENTS", "Limits", "Maximum concurrent subagents.", "3", "positive", advanced=True),
    field("CODEBOT_E2E_MAX_ROUNDS", "Limits", "Maximum E2E repair rounds.", "5", "positive", advanced=True),
    field("CODEBOT_SUBPROCESS_TIMEOUT", "Limits", "Git/GitHub subprocess timeout.", "120", "positive", advanced=True),
    field("CODEBOT_QUALITY_GATE_MAX_ROUNDS", "Limits", "Verification/internal-review repair rounds.", "3", "positive", advanced=True),
    field("CODEBOT_EMPTY_SUITE_FAILS", "Limits", "Fail the final gate when a check the agent declared ran 0 tests (auto-discovered suites are only reported).", "off", "choice", advanced=True, choices=("off", "on")),
    field("CODEBOT_ARCHIVE_MAX_ROUNDS", "Limits", "OpenSpec archive repair rounds.", "3", "positive", advanced=True),
    field("CODEBOT_CREDENTIAL_REQUESTS", "Secrets", "Let the agent ask for a missing credential, received through a one-time PrivateBin link.", "off", "choice", advanced=True, choices=("off", "on")),
    field("CODEBOT_PRIVATEBIN_INSTANCES", "Secrets", "Extra PrivateBin base URLs whose links are accepted (comma-separated, HTTPS).", "", advanced=True),
    field("CODEBOT_PRIVATEBIN_IMAGE", "Secrets", "Docker image for the temporary PrivateBin page.", "privatebin/nginx-fpm-alpine:2.0.6", advanced=True),
    field("CODEBOT_PRIVATEBIN_TTL_SECONDS", "Secrets", "Seconds the temporary secure page stays open.", "3600", "positive", advanced=True),
    field("CODEBOT_SECRET_TTL_SECONDS", "Secrets", "Seconds a received credential is kept for the task.", "14400", "positive", advanced=True),
    field("CODEBOT_MAX_STATE_FAILURES", "Limits", "Failures before WAIT_STUCK.", "5", "positive", advanced=True),
    field("CODEBOT_QUESTION_MAX_ROUNDS", "Limits", "Consecutive question round-trips.", "8", "positive", advanced=True),
    field("CODEBOT_REVIEW_WAIT_TIMEOUT", "Limits", "Automated review wait in seconds.", "2700", "positive", advanced=True),
    field("CODEBOT_REVIEW_MAX_ROUNDS", "Limits", "Automated review repair rounds.", "3", "positive", advanced=True),
    field("CODEBOT_PR_THREAD_MAX_ROUNDS", "Limits", "Human PR review repair rounds.", "3", "positive", advanced=True),
    field("CODEBOT_CONFLICT_MAX_ROUNDS", "Limits", "Merge conflict attempts.", "3", "positive", advanced=True),
    field("CODEBOT_PING_SCHEDULE", "Limits", "Decision reminder enabled (24h) or off; working check-ins are disabled.", "24h", "schedule", advanced=True),
    field("CODEBOT_SLACK_MAX_ATTACH_BYTES", "Evidence", "Maximum Slack file bytes; larger reports are compressed or split.", "104857600", "positive", advanced=True),
    field("CODEBOT_MAX_ATTACH_BYTES", "Evidence", "Maximum email attachment bytes.", "23068672", "positive", advanced=True),
    field("CODEBOT_EVIDENCE_UPLOAD", "Evidence", "Upload stitched videos to Drive.", "on", "switch"),
    field("CODEBOT_DRIVE_FOLDER_ID", "Evidence", "Destination Drive folder ID; blank uses default.", advanced=True),
    field("CODEBOT_DRIVE_SHARE_MODE", "Evidence", "Legacy setting; delivery always preserves folder permissions.", "inherited", advanced=True),
    field("CODEBOT_DRIVE_REVIEWERS", "Evidence", "Legacy setting; no individual permissions are created.", advanced=True),
    field("CODEBOT_E2E_COMPOSE_FILE", "Evidence", "Compose file to tear down after E2E timeouts.", "docker-compose.e2e.yaml", advanced=True),
    field("CODEBOT_HEARTBEAT_MAX_TICK", "Health", "Maximum plausible tick duration.", kind="positive", advanced=True),
    field("CODEBOT_HEARTBEAT_STALE", "Health", "Seconds until unhealthy heartbeat.", "360", "positive", advanced=True),
    field("CODEBOT_HEARTBEAT_HARD", "Health", "Seconds until forced restart.", "9000", "positive", advanced=True),
    field("CODEBOT_SUPERPOWERS_PLUGIN_DIR", "Runtime", "Managed Superpowers override path.", advanced=True),
    field("CODEBOT_BRIDGE_PLUGIN_DIR", "Runtime", "Managed bridge override path.", advanced=True),
    field("CODEBOT_OPENSPEC_SKILLS_DIR", "Runtime", "Managed OpenSpec skills override path.", advanced=True),
)

BY_KEY = {setting.key: setting for setting in SETTINGS}
assert len(BY_KEY) == len(SETTINGS), "duplicate configuration setting"

# Runtime aliases, and variables injected solely into subprocesses, not user knobs.
REVIEWED_NON_SETTINGS = {"GITHUB_TOKEN", "HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME",
                         "XDG_CACHE_HOME", "XDG_STATE_HOME", "PW_VIDEO",
                         "CODEBOT_SUPERVISOR_DIR", "CODEBOT_WORKER_BOOT", "CODEBOT_RUNTIME_EPOCH"}


def relevant(setting: Setting, values: dict[str, str]) -> bool:
    source = values.get("CODEBOT_TASK_SOURCE") or "gdoc"
    channel = values.get("CODEBOT_COMM_CHANNEL") or "email"
    agent = values.get("CODEBOT_AGENT") or "claude"
    if setting.group in ("GDoc", "GitHub", "Jira"):
        return setting.group.lower() == source
    if setting.group in ("Email", "Slack"):
        return setting.group.lower() == channel
    if setting.group in ("Claude", "OpenCode"):
        return setting.group.lower() == agent
    return True


def effective(setting: Setting, values: dict[str, str]) -> str:
    return values.get(setting.key, setting.default)


def normalize_value(setting: Setting, value: str) -> str:
    if setting.key == "CODEBOT_DOC_ID" and (match := re.search(r"/document/d/([a-zA-Z0-9_-]+)", value)):
        return match.group(1)
    return value


def validate_value(setting: Setting, value: str) -> str | None:
    if "\n" in value or "\r" in value or "\0" in value:
        return "must be a single-line value"
    # Every single-line value except exact 'true' is a valid disabled setting.
    if setting.kind == "exact_bool":
        return None
    if setting.key == "CODEBOT_SELF_HEAL" and not value:
        return "choose off, observe, propose or auto; use off to disable self-healing"
    if setting.choices and value and value not in setting.choices:
        return "choose one of: " + ", ".join(setting.choices)
    if setting.kind in ("positive", "nonnegative") and value:
        if not value.isascii() or not value.isdecimal():
            return "must be an integer"
        if setting.kind == "positive" and int(value) <= 0:
            return "must be greater than zero"
    if setting.kind == "email" and value and not re.fullmatch(
            r"[^\s@]+@[^\s@]+(?:\.[^\s@]+)?" if setting.key == "GIT_AUTHOR_EMAIL"
            else r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
        return "enter a valid email address"
    if setting.kind == "email_list" and value:
        if not all(re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", part.strip())
                   for part in value.split(",")):
            return "enter one or more comma-separated email addresses"
    if setting.key == "CODEBOT_DOC_ID" and value and not re.fullmatch(r"[a-zA-Z0-9_-]+", normalize_value(setting, value)):
        return "enter a Doc ID or a docs.google.com/document/d/... URL"
    if setting.key == "CODEBOT_GH_PROJECT_URL" and value and not re.fullmatch(
            r"https://github\.com/(?:orgs|users)/[^/\s]+/projects/\d+/?", value):
        return "enter a GitHub Projects v2 URL, e.g. https://github.com/orgs/team/projects/2"
    if setting.kind == "switch" and value.lower() not in ("on", "off", "true", "false", "1", "0", "yes", "no"):
        return "enter on or off"
    if setting.kind == "schedule" and value.lower() not in ("off", "none", "0", "false"):
        if not re.fullmatch(r"[1-9]\d*[smhd]?(?:\s*,\s*[1-9]\d*[smhd]?)*", value, re.I):
            return "use comma-separated durations such as 30m,1h, or off"
    if setting.key == "CODEBOT_REPO_PATH" and value and not (Path(value).is_absolute() and Path(value).is_dir()):
        return "select an absolute target directory (Git checkout or workspace)"
    if setting.key == "CODEBOT_JIRA_URL" and value and not re.fullmatch(r"https://[^/\s]+\.atlassian\.net/?", value):
        return "enter https://<site>.atlassian.net"
    if setting.key == "CODEBOT_JIRA_PICK_LABEL" and value and not re.fullmatch(r"[^\s]+", value):
        return "enter a single Jira label without spaces"
    if setting.key == "CODEBOT_SLACK_CHANNEL_ID" and value and not re.fullmatch(r"C[A-Z0-9]+", value):
        return "enter a public channel ID beginning with C"
    if setting.key == "CODEBOT_SLACK_BOT_TOKEN" and value and not value.startswith("xoxb-"):
        return "enter a Bot User OAuth Token beginning with xoxb-"
    if setting.key == "CODEBOT_SLACK_APP_TOKEN" and value and not value.startswith("xapp-"):
        return "enter a Socket Mode app-level token beginning with xapp-"
    return None


def validate(values: dict[str, str]) -> list[str]:
    errors = []
    for setting in SETTINGS:
        value = effective(setting, values)
        problem = validate_value(setting, value)
        if problem:
            errors.append(f"{setting.key}: {problem}")
    required = ["CODEBOT_REPO_PATH", "GH_TOKEN", "CODEBOT_TASK_SOURCE", "CODEBOT_COMM_CHANNEL"]
    source = values.get("CODEBOT_TASK_SOURCE") or "gdoc"
    channel = values.get("CODEBOT_COMM_CHANNEL") or "email"
    agent = values.get("CODEBOT_AGENT") or "claude"
    target = values.get("CODEBOT_REPO_PATH")
    if target and not (Path(target) / ".git").exists() and source == "github" and not values.get("CODEBOT_GH_ISSUE_REPO"):
        required.append("CODEBOT_GH_ISSUE_REPO")
    if source == "gdoc":
        required.append("CODEBOT_DOC_ID")
    elif source == "github" and not values.get("CODEBOT_GH_PROJECT_URL"):
        required.extend(("CODEBOT_GH_PROJECT_OWNER", "CODEBOT_GH_PROJECT_NUMBER"))
    elif source == "jira":
        required.extend(("CODEBOT_JIRA_URL", "CODEBOT_JIRA_PROJECT_KEY", "CODEBOT_JIRA_EMAIL",
                         "CODEBOT_JIRA_API_TOKEN", "CODEBOT_JIRA_PICK_LABEL"))
    if channel == "email":
        required.append("CODEBOT_USER_EMAIL")
    else:
        required.extend(("CODEBOT_SLACK_CHANNEL_ID", "CODEBOT_SLACK_BOT_TOKEN",
                         "CODEBOT_SLACK_APP_TOKEN"))
    if agent == "opencode":
        required.extend(("OPENCODE_PROVIDER", "OPENCODE_MODEL"))
    else:
        required.append("CLAUDE_CODE_OAUTH_TOKEN")
    for key in required:
        if not (values.get(key) or BY_KEY[key].default):
            errors.append(f"{key}: required for this selection")
    if source == "github" and values.get("CODEBOT_GH_PROJECT_URL") and not re.search(
            r"https://github\.com/(?:orgs|users)/[^/]+/projects/\d+", values["CODEBOT_GH_PROJECT_URL"]):
        errors.append("CODEBOT_GH_PROJECT_URL: expected a GitHub Projects v2 URL")
    if agent == "opencode" and values.get("OPENCODE_MODEL") and "/" not in values["OPENCODE_MODEL"]:
        errors.append("OPENCODE_MODEL: use provider/model")
    return errors
