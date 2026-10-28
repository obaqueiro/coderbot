"""Exercise the menu and section saves through Textual's actual event loop."""
from contextlib import nullcontext
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.setup_env import EnvFile

try:
    import scripts.setup_ui as setup_ui
    from scripts.setup_ui import SetupApp
    from textual.widgets import Button, Checkbox, Input, Select, Static
except ImportError:
    SetupApp = None


@unittest.skipIf(SetupApp is None, "host Textual dependency not installed")
class SetupMenu(unittest.IsolatedAsyncioTestCase):
    async def test_self_healing_enabled_disabled_and_saved_without_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            original = "# keep\nUNKNOWN=untouched\n"
            path.write_text(original)
            app = SetupApp(EnvFile(path))
            with patch("scripts.setup.subprocess.run") as process:
                async with app.run_test(size=(120, 80)) as pilot:
                    await pilot.click("#section-Self-healing")
                    await pilot.pause()
                    self.assertEqual(app.query_one("#setting-CODEBOT_SELF_HEAL", Select).value, "off")
                    self.assertIn("recreate", str(app.query_one("#description", Static).render()))
                    for mode in ("auto", "off"):
                        app.query_one("#setting-CODEBOT_SELF_HEAL", Select).value = mode
                        await pilot.pause()
                        await pilot.click("#save")
                        await pilot.pause()
                        self.assertIsNone(app.page)
                        self.assertIn("Self-healing", app.completed)
                        self.assertEqual(EnvFile(path).values["CODEBOT_SELF_HEAL"], mode)
                        await pilot.click("#section-Self-healing")
                        await pilot.pause()
                        self.assertEqual(app.query_one("#setting-CODEBOT_SELF_HEAL", Select).value, mode)
            process.assert_not_called()
            self.assertEqual(path.read_text(), original + "CODEBOT_SELF_HEAL=off\n")

    async def test_existing_self_healing_modes_are_preserved_and_discardable(self):
        for mode in ("observe", "propose", "auto"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / ".env"
                original = f"CODEBOT_SELF_HEAL={mode}\n"
                path.write_text(original)
                app = SetupApp(EnvFile(path))
                async with app.run_test(size=(120, 80)) as pilot:
                    await pilot.click("#section-Self-healing")
                    await pilot.pause()
                    self.assertEqual(app.query_one("#setting-CODEBOT_SELF_HEAL", Select).value, mode)
                    app.query_one("#setting-CODEBOT_SELF_HEAL", Select).value = "off"
                    await pilot.pause()
                    await pilot.click("#discard")
                    await pilot.pause()
                    self.assertIsNone(app.page)
                    self.assertEqual(path.read_text(), original)
                    self.assertEqual(list(path.parent.glob(".env.bak.*")), [])

    async def test_caveman_yes_no_persists_exact_boolean(self):
        for agent in ("claude", "opencode"):
            with self.subTest(agent=agent), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / ".env"
                path.write_text(f"CODEBOT_AGENT={agent}\nUSE_CAVEMAN=TRUE\n")
                app = SetupApp(EnvFile(path))
                with patch.object(setup_ui, "test_section", return_value=[]):
                    async with app.run_test(size=(120, 100)) as pilot:
                        await pilot.click("#section-Agent")
                        await pilot.pause()
                        select = app.query_one("#setting-USE_CAVEMAN", Select)
                        self.assertEqual(select.value, "false")
                        select.value = "true"
                        await pilot.pause()
                        await pilot.click("#save")
                        await pilot.pause()
                        self.assertEqual(EnvFile(path).values["USE_CAVEMAN"], "true")
                        await pilot.click("#section-Agent")
                        await pilot.pause()
                        self.assertEqual(app.query_one("#setting-USE_CAVEMAN", Select).value, "true")
                        app.query_one("#setting-USE_CAVEMAN", Select).value = "false"
                        await pilot.pause()
                        await pilot.click("#save")
                        await pilot.pause()
                        self.assertEqual(EnvFile(path).values["USE_CAVEMAN"], "false")

    async def test_agent_effort_can_be_saved_and_reset_to_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("CODEBOT_AGENT=opencode\nOPENCODE_PROVIDER=azure\n"
                            "OPENCODE_MODEL=azure/gpt-6.1-sol\n")
            app = SetupApp(EnvFile(path))
            with patch.object(setup_ui, "test_section", return_value=[]):
                async with app.run_test(size=(120, 80)) as pilot:
                    await pilot.click("#section-Agent")
                    await pilot.pause()
                    effort = app.query_one("#setting-OPENCODE_EFFORT", Select)
                    self.assertEqual(effort.value, "")
                    effort.value = "low"
                    await pilot.pause()
                    await pilot.click("#save")
                    await pilot.pause()
                    self.assertEqual(EnvFile(path).values["OPENCODE_EFFORT"], "low")
                    await pilot.click("#section-Agent")
                    await pilot.pause()
                    app.query_one("#setting-OPENCODE_EFFORT", Select).value = ""
                    await pilot.pause()
                    await pilot.click("#save")
                    await pilot.pause()
                    self.assertEqual(EnvFile(path).values["OPENCODE_EFFORT"], "")

    async def test_main_menu_syncs_existing_vm_and_checks_health(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("GH_TOKEN=keep\n")
            app = SetupApp(EnvFile(path))
            with patch.object(setup_ui, "sync_to_vm", return_value="VM copy completed and healthy") as sync, \
                 patch.object(app, "suspend", return_value=nullcontext()):
                async with app.run_test(size=(120, 80)) as pilot:
                    await pilot.pause()
                    self.assertIsNone(app.page)
                    app.query_one("#vm-host", Input).value = "azureuser@74.235.122.91"
                    app.query_one("#vm-restart", Checkbox).value = True
                    await pilot.click("#sync-vm")
                    await pilot.pause()
                    self.assertIn("healthy", str(app.query_one("#message", Static).render()))
            sync.assert_called_once_with("azureuser@74.235.122.91", code_only=True,
                                         restart=True)
            self.assertEqual(path.read_text(), "GH_TOKEN=keep\n")

    async def test_initial_vm_copy_requires_explicit_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = SetupApp(EnvFile(Path(tmp) / ".env"))
            with patch.object(setup_ui, "sync_to_vm", return_value="VM copy completed") as sync, \
                 patch.object(app, "suspend", return_value=nullcontext()):
                async with app.run_test(size=(120, 80)) as pilot:
                    await pilot.pause()
                    app.query_one("#vm-host", Input).value = "azureuser@74.235.122.91"
                    app.query_one("#vm-mode", Select).value = "initial"
                    await pilot.pause()
                    self.assertTrue(app.query_one("#vm-confirm-initial", Checkbox).display)
                    await pilot.click("#sync-vm")
                    self.assertIn("Confirm", str(app.query_one("#message", Static).render()))
                    sync.assert_not_called()
                    app.query_one("#vm-confirm-initial", Checkbox).value = True
                    await pilot.pause()
                    self.assertTrue(app.query_one("#vm-confirm-initial", Checkbox).value)
                    app.query_one("#sync-vm", Button).press()
                    await pilot.pause()
                    self.assertIn("VM copy completed", str(app.query_one("#message", Static).render()))
            sync.assert_called_once_with("azureuser@74.235.122.91", code_only=False,
                                         restart=False)

    async def test_vm_failure_is_reported_without_saving_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("GH_TOKEN=keep\n")
            app = SetupApp(EnvFile(path))
            with patch.object(setup_ui, "sync_to_vm", side_effect=RuntimeError("VM copy failed")), \
                 patch.object(app, "suspend", return_value=nullcontext()):
                async with app.run_test(size=(120, 80)) as pilot:
                    await pilot.pause()
                    app.query_one("#vm-host", Input).value = "azureuser@74.235.122.91"
                    await pilot.click("#sync-vm")
                    await pilot.pause()
                    self.assertIn("VM copy failed", str(app.query_one("#message", Static).render()))
                    self.assertFalse(app.query_one("#sync-vm", Button).disabled)
            self.assertEqual(path.read_text(), "GH_TOKEN=keep\n")

    async def test_first_screen_and_independent_save_then_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("# keep\nUNKNOWN=untouched\n")
            app = SetupApp(EnvFile(path))
            with patch.object(setup_ui, "test_section", return_value=[]) as test:
                async with app.run_test(size=(120, 80)) as pilot:
                    await pilot.pause()
                    self.assertIsNone(app.page)
                    self.assertIsNotNone(app.query_one("#section-Repository", Button))
                    await pilot.click("#section-Repository")
                    await pilot.pause()
                    app.query_one("#setting-CODEBOT_PROJECT_NAME", Input).value = "new project"
                    await pilot.click("#test-section")
                    await pilot.pause()
                    self.assertIn("passed", str(app.query_one("#message", Static).render()))
                    self.assertEqual(path.read_text(), "# keep\nUNKNOWN=untouched\n")
                    await pilot.click("#save")
                    await pilot.pause()
                    self.assertIsNone(app.page)
                    self.assertIn("Repository", app.completed)
                    self.assertEqual(test.call_count, 2)
                    self.assertEqual(EnvFile(path).values["CODEBOT_PROJECT_NAME"], "new project")
                    await pilot.click("#section-Repository")
                    await pilot.pause()
                    self.assertEqual(app.query_one("#setting-CODEBOT_PROJECT_NAME", Input).value,
                                     "new project")
                    await pilot.click("#discard")
                    await pilot.click("#exit")
                    await pilot.pause()
            self.assertIn("UNKNOWN=untouched", path.read_text())

    async def test_failed_save_preserves_draft_and_disk_then_discard(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("# keep\n")
            app = SetupApp(EnvFile(path))
            with patch.object(setup_ui, "test_section", return_value=[
                    "Slack bot lacks channels:history. Add the scope and reinstall"]):
                async with app.run_test(size=(120, 80)) as pilot:
                    await pilot.click("#section-Repository")
                    app.query_one("#setting-CODEBOT_PROJECT_NAME", Input).value = "draft"
                    await pilot.click("#save")
                    await pilot.pause()
                    self.assertEqual(app.page, 0)
                    self.assertIn("channels:history", str(app.query_one("#message", Static).render()))
                    self.assertEqual(app.values["CODEBOT_PROJECT_NAME"], "draft")
                    self.assertEqual(path.read_text(), "# keep\n")
                    await pilot.click("#discard")
                    self.assertIsNone(app.page)
                    self.assertNotIn("Repository", app.completed)

    async def test_slack_save_checks_history_scope_and_keeps_section_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("# leave untouched\n")
            app = SetupApp(EnvFile(path))
            with patch.object(setup_ui, "fetch_slack_channels", return_value=[
                    {"id": "C1", "name": "build"}]), \
                 patch("scripts.setup.fetch_slack_channels", return_value=[
                     {"id": "C1", "name": "build"}]), \
                 patch("scripts.setup._get_json", return_value={
                     "ok": False, "error": "missing_scope", "needed": "channels:history"}):
                async with app.run_test(size=(120, 80)) as pilot:
                    await pilot.click("#section-Conversation")
                    app.query_one("#setting-CODEBOT_COMM_CHANNEL", Select).value = "slack"
                    await pilot.pause()
                    app.query_one("#setting-CODEBOT_SLACK_BOT_TOKEN", Input).value = "xoxb-secret"
                    app.query_one("#setting-CODEBOT_SLACK_APP_TOKEN", Input).value = "xapp-secret"
                    for _ in range(3):
                        await pilot.click("#slack-channel-load")
                        await pilot.pause()
                        if app.slack_channels:
                            break
                    app.query_one("#setting-CODEBOT_SLACK_CHANNEL_ID", Select).value = "C1"
                    await pilot.click("#save")
                    await pilot.pause()
                    self.assertEqual(app.page, 2)
                    message = str(app.query_one("#message", Static).render())
                    self.assertIn("channels:history", message)
                    self.assertIn("reinstall", message)
                    self.assertNotIn("xoxb-secret", message)
                    self.assertEqual(path.read_text(), "# leave untouched\n")
                    self.assertNotIn("Conversation", app.completed)

    async def test_menu_full_test_uses_saved_values_and_dependency_marks_reset(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("CODEBOT_PROJECT_NAME=old\n")
            app = SetupApp(EnvFile(path))
            app.completed = {"Backlog", "Conversation", "Evidence"}
            with patch.object(setup_ui, "test_section", return_value=[]), \
                 patch.object(setup_ui, "test_all", return_value=["Agent: missing login"]) as full:
                async with app.run_test(size=(120, 80)) as pilot:
                    await pilot.click("#test-all")
                    await pilot.pause()
                    full.assert_called_once_with({"CODEBOT_PROJECT_NAME": "old"})
                    self.assertIn("Agent: missing login", str(app.query_one("#message", Static).render()))
                    await pilot.click("#section-Repository")
                    app.query_one("#setting-CODEBOT_PROJECT_NAME", Input).value = "new"
                    await pilot.click("#save")
                    await pilot.pause()
                    self.assertEqual(app.completed, {"Repository"})

    async def test_slack_tokens_channel_selection_and_stale_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = SetupApp(EnvFile(Path(tmp) / ".env"))
            channels = [[{"id": "C1", "name": "build"}], [{"id": "C2", "name": "release"}]]
            with patch.object(setup_ui, "fetch_slack_channels", side_effect=channels) as fetch:
                async with app.run_test(size=(120, 80)) as pilot:
                    await pilot.click("#section-Conversation")
                    app.query_one("#setting-CODEBOT_COMM_CHANNEL", Select).value = "slack"
                    await pilot.pause()
                    self.assertEqual(len(app.query("#fields Select")), 1)
                    await pilot.click("#slack-channel-load")
                    await pilot.pause()
                    self.assertIn("CODEBOT_SLACK_BOT_TOKEN", str(app.query_one("#message", Static).render()))
                    app.query_one("#setting-CODEBOT_SLACK_BOT_TOKEN", Input).value = "xoxb-one"
                    app.query_one("#setting-CODEBOT_SLACK_APP_TOKEN", Input).value = "xapp-one"
                    for _ in range(3):
                        await pilot.click("#slack-channel-load")
                        await pilot.pause()
                        if fetch.call_count:
                            break
                    self.assertEqual(fetch.call_count, 1, str(app.query_one("#message", Static).render()))
                    self.assertEqual(app.query_one("#setting-CODEBOT_SLACK_CHANNEL_ID", Select).value,
                                     Select.NULL)
                    app.query_one("#setting-CODEBOT_SLACK_CHANNEL_ID", Select).value = "C1"
                    app.query_one("#setting-CODEBOT_SLACK_BOT_TOKEN", Input).value = "xoxb-two"
                    for _ in range(3):
                        await pilot.click("#slack-channel-load")
                        await pilot.pause()
                        if fetch.call_count == 2:
                            break
                    self.assertEqual(fetch.call_count, 2, str(app.query_one("#message", Static).render()))
                    self.assertIs(app.query_one("#setting-CODEBOT_SLACK_CHANNEL_ID", Select).value,
                                  Select.NULL)
                    self.assertEqual(fetch.call_args.args[0]["CODEBOT_SLACK_BOT_TOKEN"], "xoxb-two")

    async def test_jira_project_then_status_dropdowns(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = SetupApp(EnvFile(Path(tmp) / ".env"))
            projects = [{"key": "ENG", "name": "Engineering"}]
            statuses = ["Ready", "In progress", "In review", "Done"]
            with patch.object(setup_ui, "fetch_jira_projects", return_value=projects), \
                 patch.object(setup_ui, "fetch_jira_statuses", return_value=statuses) as fetch:
                async with app.run_test(size=(120, 80)) as pilot:
                    await pilot.click("#section-Backlog")
                    app.query_one("#setting-CODEBOT_TASK_SOURCE", Select).value = "jira"
                    await pilot.pause()
                    app.query_one("#setting-CODEBOT_JIRA_URL", Input).value = "https://site.atlassian.net"
                    app.query_one("#setting-CODEBOT_JIRA_EMAIL", Input).value = "bot@example.org"
                    app.query_one("#setting-CODEBOT_JIRA_API_TOKEN", Input).value = "secret"
                    await pilot.click("#jira-project-load")
                    await pilot.pause()
                    self.assertIs(app.query_one("#setting-CODEBOT_JIRA_PROJECT_KEY", Select).value,
                                  Select.NULL)
                    app.query_one("#setting-CODEBOT_JIRA_PROJECT_KEY", Select).value = "ENG"
                    await pilot.click("#jira-status-load")
                    await pilot.pause()
                    self.assertIn("CODEBOT_JIRA_PICK_LABEL", str(app.query_one("#message", Static).render()))
                    fetch.assert_not_called()
                    app.query_one("#setting-CODEBOT_JIRA_PICK_LABEL", Input).value = "codebot-ready"
                    for _ in range(3):
                        await pilot.click("#jira-status-load")
                        await pilot.pause()
                        if fetch.call_count:
                            break
                    self.assertEqual(fetch.call_count, 1, str(app.query_one("#message", Static).render()))
                    self.assertIsNotNone(app.query_one("#setting-CODEBOT_JIRA_PICK_STATUS", Select))

    async def test_links_and_advanced_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = SetupApp(EnvFile(Path(tmp) / ".env"))
            with patch.object(setup_ui.webbrowser, "open", return_value=True) as browser:
                async with app.run_test(size=(120, 80)) as pilot:
                    await pilot.click("#section-Conversation")
                    app.query_one("#setting-CODEBOT_COMM_CHANNEL", Select).value = "slack"
                    await pilot.pause()
                    await pilot.click("#slack-bot-token-help")
                    await pilot.click("#slack-app-token-help")
                    self.assertEqual(browser.call_count, 2)
                    await pilot.click("#discard")
                    await pilot.click("#section-Advanced")
                    app.query_one("#search", Input).value = "CODEBOT_GH_LABEL_PREFIX"
                    await pilot.pause()
                    self.assertIsNotNone(app.query_one("#setting-CODEBOT_GH_LABEL_PREFIX", Input))


if __name__ == "__main__":
    unittest.main()
