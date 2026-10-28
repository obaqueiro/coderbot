"""Textual setup menu; shares section tests and lossless .env writer with text mode."""
import asyncio
import webbrowser

from textual.app import App, ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, Checkbox, Footer, Header, Input, Label, Select, Static

from scripts.setup import (PRIMARY_PAGES, authorize_google, authenticate_opencode,
                           fetch_jira_projects, fetch_jira_statuses, fetch_github_projects,
                           fields_for, fetch_slack_channels, sync_to_vm, test_all, test_section)
from scripts.setup_catalog import (BY_KEY, GITHUB_TOKEN_URL, JIRA_API_TOKEN_URL, JIRA_STATUS_KEYS,
                                   SLACK_APP_SETTINGS_URL, SELF_HEAL_OPTIONS, SELF_HEAL_SETUP_HELP,
                                   effective, normalize_value,
                                    validate_value)
from scripts.setup_env import EnvFile

PAGES = (*PRIMARY_PAGES, "Advanced")


class SetupApp(App[bool]):
    TITLE = "Coderbot · Setup"
    SUB_TITLE = "Jira / GitHub / GDoc  ×  Slack / Email"
    CSS = """
    Screen { background: #102b30; color: #edf8f3; }
    Header, Footer { background: #173b3d; }
    #title { height: 3; padding: 1 2; background: #1c4847; color: #c4f5df; text-style: bold; }
    #description { height: auto; min-height: 3; padding: 1 2; color: #a9cccb; }
    #search { margin: 0 2; display: none; }
    #fields { height: 1fr; margin: 0 2; padding: 0 1; scrollbar-color: #1dad97; }
    #fields Label { margin-top: 1; color: #b6e3cf; }
    #fields Input, #fields Select { width: 100%; }
    #fields .menu-row { height: 3; margin: 0 1; }
    #fields .menu-row Checkbox { width: 5; }
    #fields .menu-row Button { width: 1fr; }
    #preview { height: auto; min-height: 2; padding: 0 2; color: #a9cccb; }
    #message { height: auto; min-height: 2; padding: 0 2; color: #eaba82; }
    #actions { height: 3; align: right middle; padding: 0 2; }
    #actions Button { margin-left: 1; }
    """
    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, env: EnvFile):
        super().__init__()
        self.env = env
        self.values = dict(env.values)
        self.changes: dict[str, str] = {}
        self.page: int | None = None
        self.completed: set[str] = set()
        self.search = ""
        self.backup = None
        self.jira_statuses: list[str] = []
        self.jira_credentials: tuple[str, ...] | None = None
        self.jira_projects: list[dict[str, str]] = []
        self.jira_project_credentials: tuple[str, ...] | None = None
        self.slack_channels: list[dict[str, str]] = []
        self.slack_credentials: tuple[str, str] | None = None
        self.github_boards: list[dict[str, str]] = []
        self.github_credentials: tuple[str, ...] | None = None
        self.github_manual = False

    def _jira_credentials(self) -> tuple[str, ...]:
        return tuple(self.values.get(key, "") for key in
                     ("CODEBOT_JIRA_URL", "CODEBOT_JIRA_PROJECT_KEY", "CODEBOT_JIRA_EMAIL",
                      "CODEBOT_JIRA_API_TOKEN"))

    def _jira_statuses_current(self) -> bool:
        return bool(self.jira_statuses and self.jira_credentials == self._jira_credentials())

    def _jira_project_credentials(self) -> tuple[str, ...]:
        return tuple(self.values.get(key, "") for key in
                     ("CODEBOT_JIRA_URL", "CODEBOT_JIRA_EMAIL", "CODEBOT_JIRA_API_TOKEN"))

    def _jira_projects_current(self) -> bool:
        return bool(self.jira_projects and
                    self.jira_project_credentials == self._jira_project_credentials())

    def _slack_credentials(self) -> tuple[str, str]:
        return (self.values.get("CODEBOT_SLACK_BOT_TOKEN", ""),
                self.values.get("CODEBOT_SLACK_APP_TOKEN", ""))

    def _slack_channels_current(self) -> bool:
        return bool(self.slack_channels and self.slack_credentials == self._slack_credentials())

    def _github_credentials(self) -> tuple[str, ...]:
        return tuple(self.values.get(key, "") for key in
                     ("GH_TOKEN", "CODEBOT_REPO_PATH", "CODEBOT_GH_PROJECT_OWNER"))

    def _github_boards_current(self) -> bool:
        return bool(self.github_boards and self.github_credentials == self._github_credentials())

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(id="title")
        yield Static(id="description")
        yield Input(placeholder="Search by name or description (all settings)", id="search")
        yield VerticalScroll(id="fields")
        yield Static(id="preview")
        yield Static(id="message")
        with Horizontal(id="actions"):
            yield Button("Test", id="test-section")
            yield Button("Save and Close", variant="success", id="save")
            yield Button("Discard", id="discard")
            yield Button("Test full setup", variant="primary", id="test-all")
            yield Button("Sync to VM", variant="primary", id="sync-vm")
            yield Button("Exit", id="exit")
        yield Footer()

    async def on_mount(self) -> None:
        await self._render_page()

    def _update(self, key: str, value: str) -> None:
        value = normalize_value(BY_KEY[key], value)
        self.values[key] = value
        if self.env.values.get(key) == value:
            self.changes.pop(key, None)
        else:
            self.changes[key] = value
        self._update_preview()

    def _update_preview(self) -> None:
        if self.page is not None:
            self.query_one("#preview", Static).update(
                "Pending changes (secrets masked): " + self.env.preview(self.changes))

    def _collect(self, *, include_jira_project: bool = True,
                 include_jira_statuses: bool = True,
                 include_slack_channel: bool = True,
                 include_github_board: bool = True) -> str | None:
        if self.page is None:
            return None
        container = self.query_one("#fields", VerticalScroll)
        for setting in fields_for(PAGES[self.page], self.values, self.search):
            if setting.key in JIRA_STATUS_KEYS and not include_jira_statuses:
                continue
            if setting.key == "CODEBOT_JIRA_PROJECT_KEY" and not include_jira_project:
                continue
            if setting.key == "CODEBOT_SLACK_CHANNEL_ID" and not include_slack_channel:
                continue
            if setting.key == "CODEBOT_GH_PROJECT_URL" and not include_github_board:
                continue
            control = container.query(f"#setting-{setting.key}")
            if not control:
                continue
            widget = control.first()
            if setting.key in JIRA_STATUS_KEYS and isinstance(widget, Select) and widget.value is Select.NULL:
                return f"{setting.key}: select one of the statuses returned by Jira"
            if (setting.key == "CODEBOT_JIRA_PROJECT_KEY" and isinstance(widget, Select)
                    and widget.value is Select.NULL):
                return "CODEBOT_JIRA_PROJECT_KEY: select an accessible Jira project"
            if (setting.key == "CODEBOT_GH_PROJECT_URL" and isinstance(widget, Select)
                    and widget.value is Select.NULL):
                return "CODEBOT_GH_PROJECT_URL: choose a GitHub Projects board"
            if (setting.key == "CODEBOT_SLACK_CHANNEL_ID" and isinstance(widget, Select)
                    and widget.value is Select.NULL):
                return "CODEBOT_SLACK_CHANNEL_ID: select a public channel joined by this bot"
            value = str(widget.value)
            if error := validate_value(setting, value):
                return f"{setting.key}: {error}"
            if value != effective(setting, self.values):
                self._update(setting.key, value)
        if (include_jira_project and PAGES[self.page] == "Backlog" and
                (self.values.get("CODEBOT_TASK_SOURCE") or "gdoc") == "jira" and
                not self.values.get("CODEBOT_JIRA_PICK_LABEL", "").strip()):
            return "CODEBOT_JIRA_PICK_LABEL: enter the opt-in label required on eligible Jira issues"
        if (include_github_board and PAGES[self.page] == "Backlog" and
                (self.values.get("CODEBOT_TASK_SOURCE") or "gdoc") == "github" and
                not self.values.get("CODEBOT_GH_PROJECT_URL", "").strip()):
            return "CODEBOT_GH_PROJECT_URL: choose a board or enter its URL manually"
        return None

    async def _render_page(self) -> None:
        if self.page is None:
            self.query_one("#title", Static).update("Coderbot · Configure by section")
            self.query_one("#description", Static).update(
                "Open any section. Test and Save and Close there; each successful section is checked. "
                "Test full setup checks the entire installation. Sync to VM uses the saved "
                "configuration; Exit leaves no pending edits.")
            self.query_one("#search", Input).display = False
            self.query_one("#preview", Static).display = False
            for button in ("test-section", "save", "discard"):
                self.query_one(f"#{button}", Button).display = False
            for button in ("test-all", "sync-vm", "exit"):
                self.query_one(f"#{button}", Button).display = True
            self.query_one("#message", Static).update("")
            form = self.query_one("#fields", VerticalScroll)
            await form.remove_children()
            await form.mount(*(Horizontal(
                Checkbox(value=name in self.completed, disabled=True, id=f"checked-{name}"),
                Button(name, id=f"section-{name}"), classes="menu-row")
                for name in PAGES),
                Label("Sync to SSH VM (uses saved configuration)"),
                Input(placeholder="user@host, e.g. azureuser@74.235.122.91", id="vm-host"),
                Select([("Update code on existing VM", "code-only"),
                        ("Initial copy of app, state and target", "initial")],
                       value="code-only", id="vm-mode"),
                Checkbox("Bots stopped: confirm initial copy of configuration and state",
                         id="vm-confirm-initial"),
                Checkbox("Recreate container and check health after sync", id="vm-restart"))
            self.query_one("#vm-confirm-initial", Checkbox).display = False
            return
        page = PAGES[self.page]
        self.query_one("#title", Static).update(f"{self.page + 1} / {len(PAGES)}    {page}")
        self.query_one("#search", Input).display = page == "Advanced"
        self.query_one("#preview", Static).display = True
        for button in ("test-section", "save", "discard"):
            self.query_one(f"#{button}", Button).display = True
        for button in ("test-all", "sync-vm", "exit"):
            self.query_one(f"#{button}", Button).display = False
        self.query_one("#message", Static).update("")
        description = ("All configuration keys are searchable, including settings for inactive integrations."
                       if page == "Advanced" else
                       SELF_HEAL_SETUP_HELP if page == "Self-healing" else
                       "Test this section, then Save and Close to persist it. Discard returns to the menu.")
        self.query_one("#description", Static).update(description)
        form = self.query_one("#fields", VerticalScroll)
        await form.remove_children()
        widgets = []
        for setting in fields_for(page, self.values, self.search):
            if (setting.key == "CODEBOT_GH_PROJECT_URL" and
                    (self.values.get("CODEBOT_TASK_SOURCE") or "gdoc") == "github" and
                    not self.github_manual and not self._github_boards_current()):
                continue
            if (setting.key == "CODEBOT_JIRA_PROJECT_KEY" and
                    (self.values.get("CODEBOT_TASK_SOURCE") or "gdoc") == "jira" and
                    not self._jira_projects_current()):
                continue
            if (setting.key in JIRA_STATUS_KEYS and
                    (self.values.get("CODEBOT_TASK_SOURCE") or "gdoc") == "jira" and
                    not self._jira_statuses_current()):
                continue
            if (setting.key == "CODEBOT_SLACK_CHANNEL_ID" and
                    (self.values.get("CODEBOT_COMM_CHANNEL") or "email") == "slack" and
                    not self._slack_channels_current()):
                continue
            widgets.append(Label(f"{setting.key} · {setting.help}"))
            value = effective(setting, self.values)
            if (setting.key == "CODEBOT_GH_PROJECT_URL" and
                    (self.values.get("CODEBOT_TASK_SOURCE") or "gdoc") == "github" and
                    not self.github_manual):
                urls = {board["url"] for board in self.github_boards}
                widgets.append(Select([(f"{board['title']} ({board['url']})", board["url"])
                                       for board in self.github_boards],
                                      value=value if value in urls else Select.NULL,
                                      prompt="Choose a GitHub Projects board",
                                      id=f"setting-{setting.key}"))
            elif (setting.key == "CODEBOT_JIRA_PROJECT_KEY" and
                    (self.values.get("CODEBOT_TASK_SOURCE") or "gdoc") == "jira"):
                keys = {project["key"] for project in self.jira_projects}
                widgets.append(Select([(f"{project['name']} ({project['key']})", project["key"])
                                       for project in self.jira_projects],
                                      value=value if value in keys else Select.NULL,
                                      prompt="Choose a Jira project",
                                      id=f"setting-{setting.key}"))
            elif (setting.key in JIRA_STATUS_KEYS and
                    (self.values.get("CODEBOT_TASK_SOURCE") or "gdoc") == "jira"):
                selected = next((name for name in self.jira_statuses
                                 if name.casefold() == value.casefold()), Select.NULL)
                widgets.append(Select([(name, name) for name in self.jira_statuses],
                                      value=selected, prompt="Choose a Jira status",
                                      id=f"setting-{setting.key}"))
            elif (setting.key == "CODEBOT_SLACK_CHANNEL_ID" and
                  (self.values.get("CODEBOT_COMM_CHANNEL") or "email") == "slack"):
                joined_ids = {channel["id"] for channel in self.slack_channels}
                widgets.append(Select([(f"#{channel['name']} ({channel['id']})", channel["id"])
                                       for channel in self.slack_channels],
                                      value=value if value in joined_ids else Select.NULL,
                                      prompt="Choose a joined public channel",
                                      id=f"setting-{setting.key}"))
            elif setting.key == "CODEBOT_SELF_HEAL":
                widgets.append(Select(SELF_HEAL_OPTIONS,
                                      value=value if value in setting.choices else "off",
                                      allow_blank=False, id=f"setting-{setting.key}"))
            elif setting.kind == "exact_bool":
                widgets.append(Select([("YES", "true"), ("NO", "false")],
                                      value="true" if value == "true" else "false",
                                      allow_blank=False, id=f"setting-{setting.key}"))
            elif setting.choices:
                widgets.append(Select([(choice or "Default (OpenCode/provider)", choice) for choice in setting.choices],
                                      value=value if value in setting.choices else setting.choices[0],
                                      id=f"setting-{setting.key}"))
            else:
                widgets.append(Input(value=value, password=setting.secret,
                                     placeholder=setting.default or setting.help,
                                     id=f"setting-{setting.key}"))
            if setting.key == "CODEBOT_JIRA_API_TOKEN":
                widgets.append(Button("Open Atlassian API token page ↗", id="jira-token-help"))
                widgets.append(Button("Load accessible Jira projects", id="jira-project-load",
                                      variant="primary"))
            if setting.key == "CODEBOT_JIRA_PROJECT_KEY":
                widgets.append(Button("Load project workflow statuses", id="jira-status-load",
                                      variant="primary"))
            if setting.key == "CODEBOT_SLACK_BOT_TOKEN":
                widgets.append(Button("Create bot token (OAuth & Permissions) ↗",
                                      id="slack-bot-token-help"))
            if setting.key == "CODEBOT_SLACK_APP_TOKEN":
                widgets.append(Button("Create app-level token (Basic Information) ↗",
                                      id="slack-app-token-help"))
                widgets.append(Button("Load joined public channels", id="slack-channel-load",
                                      variant="primary"))
            if setting.key == "GH_TOKEN":
                widgets.append(Button("Create GitHub token ↗", id="github-token-help"))
        if page == "Backlog" and (self.values.get("CODEBOT_TASK_SOURCE") or "gdoc") == "github":
            widgets.append(Button("Load accessible GitHub boards", id="github-board-load",
                                  variant="primary"))
            widgets.append(Button("Choose from boards" if self.github_manual else
                                  "Enter a board URL manually", id="github-board-manual"))
            widgets.append(Static(
                "A manual URL can select a board belonging to a different owner; "
                "the final Test checks access using GH_TOKEN."
                if self.github_manual else
                f"Loaded {len(self.github_boards)} accessible boards. Choose one above."
                if self._github_boards_current() else
                "Set GH_TOKEN and a target repo in Repository, then load accessible boards."))
        if page == "Backlog" and (self.values.get("CODEBOT_TASK_SOURCE") or "gdoc") == "jira":
            widgets.append(Static(
                f"Loaded {len(self.jira_statuses)} workflow statuses from Jira. Choose the four mappings above."
                if self._jira_statuses_current() else
                "Select an accessible project, enter its opt-in label, then load its workflow statuses."
                if self._jira_projects_current() else
                "Enter the Jira site, email and token, then load accessible projects."))
        if page == "Conversation" and (self.values.get("CODEBOT_COMM_CHANNEL") or "email") == "slack":
            widgets.append(Static(
                f"Loaded {len(self.slack_channels)} joined public channels. Choose one above."
                if self._slack_channels_current() else
                "Create a bot token and an app-level token from the SAME Slack app, invite the bot "
                "to a public channel, then load the joined channels."))
        if (page == "Backlog" and (self.values.get("CODEBOT_TASK_SOURCE") or "gdoc") == "gdoc"
                or page == "Conversation" and (self.values.get("CODEBOT_COMM_CHANNEL") or "email") == "email"
                or page == "Evidence" and (self.values.get("CODEBOT_EVIDENCE_UPLOAD") or "on").lower()
                not in ("off", "false", "0", "no")):
            widgets.append(Button("Authorize Google (opens browser)", id="authorize-google"))
        if page == "Agent" and (self.values.get("CODEBOT_AGENT") or "claude") == "opencode":
            widgets.append(Button("Authenticate OpenCode provider", id="authenticate-opencode"))
        await form.mount(*widgets)
        self._update_preview()

    async def _load_jira_projects(self) -> None:
        self.query_one("#message", Static).update("Authenticating with Jira and loading accessible projects...")
        try:
            projects = await asyncio.to_thread(fetch_jira_projects, dict(self.values))
        except RuntimeError as error:
            self.jira_projects = []
            self.jira_project_credentials = None
            self.jira_statuses = []
            self.jira_credentials = None
            self.query_one("#message", Static).update(
                f"Jira: {error}. Check the site, account and API token, then retry.")
            return
        self.jira_projects = projects
        self.jira_project_credentials = self._jira_project_credentials()
        self.jira_statuses = []  # reload workflow statuses after choosing a project
        self.jira_credentials = None
        await self._render_page()
        self.query_one("#message", Static).update(
            f"Loaded {len(projects)} Jira projects. Choose one to configure its workflow.")

    async def _load_jira_statuses(self) -> None:
        if not self._jira_projects_current():
            self.query_one("#message", Static).update("Load accessible Jira projects before their statuses.")
            return
        self.query_one("#message", Static).update("Connecting to Jira and loading project statuses...")
        try:
            names = await asyncio.to_thread(fetch_jira_statuses, dict(self.values))
        except RuntimeError as error:
            self.jira_statuses = []
            self.jira_credentials = None
            self.query_one("#message", Static).update(f"Jira: {error}. Check the site, project, email and token, then retry.")
            return
        self.jira_statuses = names
        self.jira_credentials = self._jira_credentials()
        await self._render_page()
        self.query_one("#message", Static).update(
            f"Loaded {len(names)} Jira workflow statuses. Confirm the four dropdown mappings before continuing.")

    async def _load_slack_channels(self) -> None:
        for key in ("CODEBOT_SLACK_BOT_TOKEN", "CODEBOT_SLACK_APP_TOKEN"):
            if not self.values.get(key, "").strip():
                self.query_one("#message", Static).update(
                    f"Enter {key} first. Create both tokens in this instance's Slack app, then retry.")
                return
        self.query_one("#message", Static).update("Checking both Slack tokens and loading joined public channels...")
        try:
            channels = await asyncio.to_thread(fetch_slack_channels, dict(self.values))
        except RuntimeError as error:
            self.slack_channels = []
            self.slack_credentials = None
            self.query_one("#message", Static).update(
                f"Slack: {error}. Check both tokens, invite the bot or retry.")
            return
        self.slack_channels = channels
        self.slack_credentials = self._slack_credentials()
        await self._render_page()
        self.query_one("#message", Static).update(
            f"Loaded {len(channels)} joined public channels. Select one before continuing.")

    async def _load_github_boards(self) -> None:
        self.query_one("#message", Static).update("Checking GH_TOKEN and loading Projects v2 boards...")
        try:
            boards = await asyncio.to_thread(fetch_github_projects, dict(self.values))
        except RuntimeError as error:
            self.github_boards = []
            self.github_credentials = None
            self.query_one("#message", Static).update(
                f"GitHub: {error}. Check GH_TOKEN, project scope and owner, then retry.")
            return
        self.github_boards = boards
        self.github_credentials = self._github_credentials()
        self.github_manual = False
        await self._render_page()
        self.query_one("#message", Static).update(
            f"Loaded {len(boards)} Projects v2 boards. Choose one or use a manual URL.")

    async def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "vm-mode" and self.page is None:
            confirm = self.query("#vm-confirm-initial")
            if confirm:
                confirm.first().display = event.value == "initial"
            return
        if not event.select.id or not event.select.id.startswith("setting-"):
            return
        key = event.select.id.removeprefix("setting-")
        value = str(event.value)
        if key in ("CODEBOT_TASK_SOURCE", "CODEBOT_COMM_CHANNEL", "CODEBOT_AGENT") and value != effective(BY_KEY[key], self.values):
            self._collect()
            self._update(key, value)
            await self._render_page()

    async def on_input_changed(self, event: Input.Changed) -> None:
        if self.page is None:
            return
        if event.input.id == "search" and PAGES[self.page] == "Advanced":
            self._collect()
            self.search = event.value
            await self._render_page()
        elif event.input.id and event.input.id.startswith("setting-"):
            key = event.input.id.removeprefix("setting-")
            if event.value != effective(BY_KEY[key], self.values):
                self._update(key, event.value)

    async def _discard_section(self) -> None:
        self.values = dict(self.env.values)
        self.changes = {}
        self.page = None
        self.search = ""
        await self._render_page()

    def _invalidate_after_save(self, section: str, changed: dict[str, str]) -> None:
        if not changed:
            return
        dependents = {
            "Repository": {"Backlog", "Conversation", "Agent", "Evidence"},
            "Backlog": {"Conversation", "Evidence"},
            "Conversation": {"Backlog", "Evidence"},
            "Evidence": {"Backlog", "Conversation"},
            "Agent": set(),
            "Self-healing": set(),
            "Advanced": set(PRIMARY_PAGES),
        }
        self.completed.difference_update(dependents[section])

    async def _test_section(self, *, save: bool) -> None:
        if self.page is None:
            return
        section = PAGES[self.page]
        if section == "Backlog" and (self.values.get("CODEBOT_TASK_SOURCE") or "gdoc") == "jira":
            if error := self._collect(include_jira_project=False, include_jira_statuses=False):
                self.query_one("#message", Static).update(error)
                return
            if not self._jira_projects_current():
                await self._load_jira_projects()
                return
            if error := self._collect(include_jira_statuses=False):
                self.query_one("#message", Static).update(error)
                return
            if not self._jira_statuses_current():
                await self._load_jira_statuses()
                return
        if section == "Backlog" and (self.values.get("CODEBOT_TASK_SOURCE") or "gdoc") == "github":
            if error := self._collect(include_github_board=self.github_manual):
                self.query_one("#message", Static).update(error)
                return
            if not self.github_manual and not self._github_boards_current():
                await self._load_github_boards()
                return
        if section == "Conversation" and (self.values.get("CODEBOT_COMM_CHANNEL") or "email") == "slack":
            if error := self._collect(include_slack_channel=False):
                self.query_one("#message", Static).update(error)
                return
            if not self._slack_channels_current():
                await self._load_slack_channels()
                return
        if error := self._collect():
            self.query_one("#message", Static).update(error)
            return
        self.query_one("#message", Static).update(f"Testing {section}...")
        errors = await asyncio.to_thread(test_section, section, dict(self.values),
                                         set(self.changes))
        if errors:
            self.query_one("#message", Static).update(
                f"{section} failed; nothing was saved:\n- " + "\n- ".join(errors))
            return
        if not save:
            self.query_one("#message", Static).update(
                f"{section} passed. Select Save and Close to persist this section.")
            return
        try:
            self.backup = self.env.save(self.changes, validator=lambda _values: [])
        except (OSError, ValueError) as error:
            self.query_one("#message", Static).update(f"{section} could not be saved: {error}")
            return
        changed = dict(self.changes)
        self._invalidate_after_save(section, changed)
        self.completed.add(section)
        backup = self.backup
        self.env = EnvFile(self.env.path)
        await self._discard_section()
        self.query_one("#message", Static).update(
            f"{section} passed and saved to {self.env.path}. "
            + (f"Backup: {backup}. " if backup else "")
            + "Checkmark updated; dependent sections may need retesting.")

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        action = event.button.id
        if action == "sync-vm" and self.page is None:
            if self.changes:
                self.query_one("#message", Static).update("Save or discard pending edits before syncing.")
                return
            host = self.query_one("#vm-host", Input).value
            code_only = self.query_one("#vm-mode", Select).value == "code-only"
            if not code_only and not self.query_one("#vm-confirm-initial", Checkbox).value:
                self.query_one("#message", Static).update(
                    "Confirm the initial copy of configuration and state before syncing.")
                return
            restart = self.query_one("#vm-restart", Checkbox).value
            button = self.query_one("#sync-vm", Button)
            button.disabled = True
            self.query_one("#message", Static).update("Syncing with the VM; output will appear in the terminal...")
            try:
                with self.suspend():
                    result = await asyncio.to_thread(sync_to_vm, host, code_only=code_only,
                                                     restart=restart)
            except (ValueError, RuntimeError) as error:
                self.query_one("#message", Static).update(str(error))
            else:
                self.query_one("#message", Static).update(result)
            finally:
                button.disabled = False
            return
        if action and action.startswith("section-"):
            self.values = dict(self.env.values)
            self.changes = {}
            self.search = ""
            self.page = PAGES.index(action.removeprefix("section-"))
            await self._render_page()
            return
        if action == "exit" and self.page is None:
            self.exit(True)
            return
        if action == "test-all" and self.page is None:
            self.query_one("#message", Static).update("Testing the complete saved configuration...")
            errors = await asyncio.to_thread(test_all, dict(self.env.values))
            self.query_one("#message", Static).update(
                "Full test failed:\n- " + "\n- ".join(errors) if errors else
                "Full test passed: the saved configuration is ready.")
            return
        if action == "discard" and self.page is not None:
            await self._discard_section()
            return
        if action == "jira-token-help":
            opened = webbrowser.open(JIRA_API_TOKEN_URL)
            self.query_one("#message", Static).update(
                "Opened token page in your browser." if opened else
                f"Open this URL to create a token: {JIRA_API_TOKEN_URL}")
            return
        if action == "jira-project-load":
            if error := self._collect(include_jira_project=False, include_jira_statuses=False):
                self.query_one("#message", Static).update(error)
                return
            await self._load_jira_projects()
            return
        if action in ("slack-bot-token-help", "slack-app-token-help"):
            opened = webbrowser.open(SLACK_APP_SETTINGS_URL)
            location = ("OAuth & Permissions → Bot User OAuth Token (xoxb-...)"
                        if action == "slack-bot-token-help" else
                        "Basic Information → App-Level Tokens (xapp-..., connections:write)")
            self.query_one("#message", Static).update(
                f"{'Opened' if opened else 'Open'} {SLACK_APP_SETTINGS_URL}; "
                f"select this instance's app, then {location}.")
            return
        if action == "github-token-help":
            opened = webbrowser.open(GITHUB_TOKEN_URL)
            self.query_one("#message", Static).update(
                f"{'Opened' if opened else 'Open'} {GITHUB_TOKEN_URL}; for Projects v2 "
                "also grant project write permission and authorize org SSO if required.")
            return
        if action == "jira-status-load":
            if error := self._collect(include_jira_statuses=False):
                self.query_one("#message", Static).update(error)
                return
            await self._load_jira_statuses()
            return
        if action == "slack-channel-load":
            if error := self._collect(include_slack_channel=False):
                self.query_one("#message", Static).update(error)
                return
            await self._load_slack_channels()
            return
        if action == "github-board-load":
            if error := self._collect(include_github_board=False):
                self.query_one("#message", Static).update(error)
                return
            await self._load_github_boards()
            return
        if action == "github-board-manual":
            if error := self._collect(include_github_board=False):
                self.query_one("#message", Static).update(error)
                return
            self.github_manual = not self.github_manual
            await self._render_page()
            return
        if action == "authorize-google":
            if error := self._collect():
                self.query_one("#message", Static).update(error)
                return
            try:
                with self.suspend():
                    await asyncio.to_thread(authorize_google, dict(self.values))
            except RuntimeError as error:
                self.query_one("#message", Static).update(str(error))
            else:
                self.query_one("#message", Static).update("Google authorization completed. Test this section again.")
            return
        if action == "authenticate-opencode":
            if error := self._collect():
                self.query_one("#message", Static).update(error)
                return
            try:
                with self.suspend():
                    await asyncio.to_thread(authenticate_opencode, dict(self.values))
            except RuntimeError as error:
                self.query_one("#message", Static).update(str(error))
            else:
                self.query_one("#message", Static).update(
                    "OpenCode authentication completed. Enter a model from the provider's list and Test Agent.")
            return
        if action in ("test-section", "save") and self.page is not None:
            await self._test_section(save=action == "save")
            return

    async def action_cancel(self) -> None:
        if self.page is None:
            self.exit(True)
        else:
            await self._discard_section()

def run(env: EnvFile) -> bool:
    app = SetupApp(env)
    return bool(app.run())
