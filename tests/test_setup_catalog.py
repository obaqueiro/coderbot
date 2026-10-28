"""Runtime environment variables must remain editable and documented in setup."""
import ast
import re
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from scripts.setup_catalog import BY_KEY, REVIEWED_NON_SETTINGS, normalize_value, relevant, validate
from scripts.setup_env import EnvFile
from scripts import setup as guided_setup

ROOT = Path(__file__).resolve().parent.parent


def runtime_names() -> set[str]:
    found = set()
    for filename in (ROOT / "src").glob("*.py"):
        tree = ast.parse(filename.read_text())
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "get" and isinstance(node.func.value, ast.Attribute)
                    and node.func.value.attr == "environ" and node.args
                    and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
                found.add(node.args[0].value)
            if (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute)
                    and node.value.attr == "environ" and isinstance(node.slice, ast.Constant)
                    and isinstance(node.slice.value, str)):
                found.add(node.slice.value)
    for filename in ("scripts/entrypoint.sh", "scripts/healthcheck.sh", "docker-compose.yml",
                     "docker-compose.ec2.yml", "deploy/aws/docker-compose.aws.yml"):
        found.update(re.findall(r"\$\{([A-Z][A-Z0-9_]*)", (ROOT / filename).read_text()))
    return {name for name in found if name == "USE_CAVEMAN" or
            name.startswith(("CODEBOT_", "CLAUDE_", "OPENCODE_", "GH_", "GIT_"))}


class CatalogAudit(unittest.TestCase):
    def test_every_runtime_knob_is_in_the_setup_catalog(self):
        self.assertEqual(runtime_names() - BY_KEY.keys() - REVIEWED_NON_SETTINGS, set())

    def test_catalog_is_exhaustive_and_documented_in_env_example(self):
        documented = set(re.findall(r"^([A-Z][A-Z0-9_]*)=", (ROOT / ".env.example").read_text(), re.M))
        self.assertEqual(BY_KEY.keys() - documented, set())
        self.assertEqual(documented - BY_KEY.keys() - REVIEWED_NON_SETTINGS, set())

    def test_inactive_integration_settings_are_still_available(self):
        self.assertIn("CODEBOT_GH_LABEL_PREFIX", BY_KEY)
        self.assertIn("CODEBOT_SLACK_APP_TOKEN", BY_KEY)
        self.assertFalse(relevant(BY_KEY["CODEBOT_SLACK_APP_TOKEN"],
                                  {"CODEBOT_COMM_CHANNEL": "email"}))
        self.assertTrue(relevant(BY_KEY["CODEBOT_SLACK_APP_TOKEN"],
                                  {"CODEBOT_COMM_CHANNEL": "slack"}))

    def test_opencode_effort_is_editable_in_agent_and_validated(self):
        from scripts.setup_catalog import validate_value
        setting = BY_KEY["OPENCODE_EFFORT"]
        self.assertIn(setting, guided_setup.fields_for("Agent", {"CODEBOT_AGENT": "opencode"}))
        self.assertNotIn(setting, guided_setup.fields_for("Agent", {"CODEBOT_AGENT": "claude"}))
        for value in ("", "none", "minimal", "low", "medium", "high", "xhigh"):
            self.assertIsNone(validate_value(setting, value))
        self.assertIsNotNone(validate_value(setting, "typo"))

    def test_self_healing_has_main_section_with_safe_default_and_all_modes(self):
        from scripts.setup_catalog import SELF_HEAL_OPTIONS, validate_value
        setting = BY_KEY["CODEBOT_SELF_HEAL"]
        self.assertIn("Self-healing", guided_setup.PRIMARY_PAGES)
        self.assertEqual(guided_setup.fields_for("Self-healing", {}), [setting])
        self.assertIn(setting, guided_setup.fields_for("Advanced", {}, "self_heal"))
        self.assertEqual(setting.default, "off")
        self.assertEqual(setting.choices, tuple(mode for _label, mode in SELF_HEAL_OPTIONS))
        for mode in setting.choices:
            with self.subTest(mode=mode):
                self.assertIsNone(validate_value(setting, mode))
                self.assertEqual(guided_setup.test_section("Self-healing", {setting.key: mode}), [])
        for invalid in ("", "on", "true", "wrong"):
            self.assertIsNotNone(validate_value(setting, invalid))

    def test_pasted_doc_url_and_multiple_trusted_email_addresses(self):
        from scripts.setup_catalog import validate_value
        doc = BY_KEY["CODEBOT_DOC_ID"]
        self.assertIsNone(validate_value(doc, "https://docs.google.com/document/d/Abc123/edit"))
        self.assertEqual(normalize_value(doc, "https://docs.google.com/document/d/Abc123/edit"), "Abc123")
        self.assertIsNone(validate_value(BY_KEY["CODEBOT_USER_EMAIL"],
                                         "one@example.com,two@example.org"))
        self.assertIsNotNone(validate_value(BY_KEY["CODEBOT_USER_EMAIL"],
                                            "one@example.com,not-an-email"))

    def test_validations_are_conditional(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "project"
            repo.mkdir()
            (repo / ".git").mkdir()
            values = {"CODEBOT_REPO_PATH": str(repo), "GH_TOKEN": "token",
                      "CODEBOT_TASK_SOURCE": "jira", "CODEBOT_COMM_CHANNEL": "slack",
                      "CODEBOT_JIRA_URL": "https://site.atlassian.net",
                      "CODEBOT_JIRA_PROJECT_KEY": "APP", "CODEBOT_JIRA_EMAIL": "bot@example.org",
                      "CODEBOT_JIRA_API_TOKEN": "secret", "CODEBOT_JIRA_PICK_LABEL": "codebot-ready",
                      "CODEBOT_SLACK_CHANNEL_ID": "C123",
                      "CODEBOT_SLACK_BOT_TOKEN": "xoxb-key", "CODEBOT_SLACK_APP_TOKEN": "xapp-key",
                      "CLAUDE_CODE_OAUTH_TOKEN": "token"}
            self.assertEqual(validate(values), [])
            del values["CODEBOT_SLACK_APP_TOKEN"]
            self.assertIn("CODEBOT_SLACK_APP_TOKEN", "\n".join(validate(values)))
            values["CODEBOT_SLACK_APP_TOKEN"] = "xapp-key"
            del values["CODEBOT_JIRA_PICK_LABEL"]
            self.assertIn("CODEBOT_JIRA_PICK_LABEL", "\n".join(validate(values)))

    def test_google_scopes_follow_selected_source_channel_and_upload(self):
        self.assertEqual(guided_setup._google_scopes({
            "CODEBOT_TASK_SOURCE": "jira", "CODEBOT_COMM_CHANNEL": "slack",
            "CODEBOT_EVIDENCE_UPLOAD": "off"}), [])
        self.assertEqual(len(guided_setup._google_scopes({
            "CODEBOT_TASK_SOURCE": "gdoc", "CODEBOT_COMM_CHANNEL": "slack",
            "CODEBOT_EVIDENCE_UPLOAD": "off"})), 2)
        self.assertEqual(len(guided_setup._google_scopes({
            "CODEBOT_TASK_SOURCE": "jira", "CODEBOT_COMM_CHANNEL": "email",
            "CODEBOT_EVIDENCE_UPLOAD": "on"})), 2)

    def test_live_preflight_checks_jira_statuses_and_slack_app_without_leaking_tokens(self):
        values = {"CODEBOT_TASK_SOURCE": "jira", "CODEBOT_COMM_CHANNEL": "slack",
                  "CODEBOT_JIRA_URL": "https://site.atlassian.net",
                  "CODEBOT_JIRA_EMAIL": "bot@example.org", "CODEBOT_JIRA_API_TOKEN": "secret",
                  "CODEBOT_JIRA_PROJECT_KEY": "APP", "CODEBOT_SLACK_BOT_TOKEN": "xoxb-secret",
                  "CODEBOT_SLACK_APP_TOKEN": "xapp-secret", "CODEBOT_SLACK_CHANNEL_ID": "C123"}
        responses = [{"accountId": "A"}, {"key": "APP"}, {"statuses": []}]
        statuses = [{"statuses": [{"name": n} for n in ("Ready", "In progress", "In review", "Done")]}]
        responses = [responses[0], responses[1], statuses,
                     {"ok": True, "user_id": "U1", "team": "Acme"},
                      {"ok": True, "url": "wss://slack"},
                      {"ok": True, "channels": [{"id": "C123", "name": "build",
                                                "is_channel": True, "is_private": False,
                                                "is_member": True}]}, {"ok": True}]
        class Result:
            def __init__(self, value):
                self.value = value
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                pass
            def read(self):
                import json
                return json.dumps(self.value).encode()
        with patch.object(guided_setup.urllib.request, "urlopen",
                          side_effect=[Result(value) for value in responses]) as calls:
            self.assertEqual(guided_setup.preflight(values), [])
        self.assertEqual(calls.call_count, 7)
        values["CODEBOT_JIRA_DONE_STATUS"] = "Not a status"
        values["CODEBOT_COMM_CHANNEL"] = "email"
        with patch.object(guided_setup.urllib.request, "urlopen",
                          side_effect=[Result(value) for value in responses[:3]]):
            errors = guided_setup.preflight(values)
        self.assertIn("CODEBOT_JIRA_DONE_STATUS", "\n".join(errors))

    def test_slack_tokens_precede_channel_selection_and_guides_to_both_token_pages(self):
        from scripts.setup_catalog import SLACK_APP_SETTINGS_URL
        choices = [setting.key for setting in guided_setup.fields_for(
            "Conversation", {"CODEBOT_COMM_CHANNEL": "slack"})]
        self.assertEqual(choices, ["CODEBOT_COMM_CHANNEL", "CODEBOT_SLACK_BOT_TOKEN",
                                   "CODEBOT_SLACK_APP_TOKEN", "CODEBOT_SLACK_CHANNEL_ID"])
        self.assertIn(SLACK_APP_SETTINGS_URL, BY_KEY["CODEBOT_SLACK_BOT_TOKEN"].help)
        self.assertIn("OAuth & Permissions", BY_KEY["CODEBOT_SLACK_BOT_TOKEN"].help)
        self.assertIn(SLACK_APP_SETTINGS_URL, BY_KEY["CODEBOT_SLACK_APP_TOKEN"].help)
        self.assertIn("connections:write", BY_KEY["CODEBOT_SLACK_APP_TOKEN"].help)

    def test_slack_channel_discovery_paginates_and_excludes_unjoined_or_private_channels(self):
        values = {"CODEBOT_SLACK_BOT_TOKEN": "xoxb-secret",
                  "CODEBOT_SLACK_APP_TOKEN": "xapp-secret"}
        def channel(channel_id, name, *, joined=True, private=False, archived=False):
            return {"id": channel_id, "name": name, "is_channel": True,
                    "is_private": private, "is_archived": archived, "is_member": joined}
        responses = [{"ok": True, "team": "Acme"}, {"ok": True, "url": "wss://slack"},
                     {"ok": True, "channels": [channel("C2", "release"),
                                                channel("C3", "other", joined=False),
                                                channel("G4", "private", private=True)],
                      "response_metadata": {"next_cursor": "cursor-two"}},
                     {"ok": True, "channels": [channel("C1", "build"),
                                                channel("C5", "old", archived=True)],
                      "response_metadata": {"next_cursor": ""}}]
        with patch.object(guided_setup, "_get_json", side_effect=responses) as request:
            self.assertEqual(guided_setup.fetch_slack_channels(values),
                             [{"id": "C1", "name": "build"},
                              {"id": "C2", "name": "release"}])
        self.assertIn("cursor=cursor-two", request.call_args_list[3].args[0])
        self.assertTrue(all("secret" not in call.args[0] for call in request.call_args_list))

    def test_slack_discovery_reports_missing_scopes_and_no_joined_channels(self):
        values = {"CODEBOT_SLACK_BOT_TOKEN": "xoxb-secret",
                  "CODEBOT_SLACK_APP_TOKEN": "xapp-secret"}
        with patch.object(guided_setup, "_get_json", return_value={
                "ok": True, "_granted_scopes": "channels:read,channels:history"}) as request:
            with self.assertRaisesRegex(RuntimeError, "chat:write, files:write"):
                guided_setup.fetch_slack_channels(values)
            request.assert_called_once()
        with patch.object(guided_setup, "_get_json", side_effect=[
                {"ok": True}, {"ok": True}, {"ok": False, "error": "missing_scope"}]):
            with self.assertRaisesRegex(RuntimeError, "channels:read"):
                guided_setup.fetch_slack_channels(values)
        with patch.object(guided_setup, "_get_json", side_effect=[
                {"ok": True, "team": "Acme"}, {"ok": True},
                {"ok": True, "channels": [{"id": "C1", "name": "build",
                                            "is_channel": True, "is_member": False}]}]):
            with self.assertRaisesRegex(RuntimeError, "invite.*public channel"):
                guided_setup.fetch_slack_channels(values)
        with patch.object(guided_setup, "_get_json") as request:
            with self.assertRaisesRegex(RuntimeError, "xoxb"):
                guided_setup.fetch_slack_channels(values | {"CODEBOT_SLACK_BOT_TOKEN": "xoxp-user"})
            request.assert_not_called()

    def test_slack_selected_channel_error_explains_membership_or_missing_scope(self):
        values = {"CODEBOT_SLACK_CHANNEL_ID": "C123",
                  "CODEBOT_SLACK_BOT_TOKEN": "xoxb-secret"}
        with patch.object(guided_setup, "_get_json", return_value={
                "ok": True, "channel": {"is_channel": True, "is_private": False,
                                        "is_member": False, "name": "engineering"}}):
            message = guided_setup._slack_channel_error(values)
        self.assertIn("not a member of #engineering", message)
        self.assertIn("Add people/apps", message)
        self.assertNotIn("xoxb-secret", message)
        with patch.object(guided_setup, "_get_json", return_value={
                "ok": False, "error": "missing_scope", "needed": "channels:read"}):
            message = guided_setup._slack_channel_error(values)
        self.assertIn("channels:read", message)
        self.assertIn("reinstall", message)
        with patch.object(guided_setup, "_get_json", return_value={
                "ok": True, "channel": {"is_channel": True, "is_archived": True,
                                         "is_member": False}}):
            self.assertIn("archived", guided_setup._slack_channel_error(values))
        with patch.object(guided_setup, "_get_json", return_value={
                "ok": True, "channel": {"is_channel": True, "is_private": True,
                                         "is_member": True}}):
            self.assertIn("not a public channel", guided_setup._slack_channel_error(values))

    def test_preflight_explains_unjoined_selected_channel_instead_of_generic_error(self):
        values = {"CODEBOT_COMM_CHANNEL": "slack", "CODEBOT_SLACK_CHANNEL_ID": "C999",
                  "CODEBOT_SLACK_BOT_TOKEN": "xoxb-secret",
                  "CODEBOT_SLACK_APP_TOKEN": "xapp-secret"}
        with patch.object(guided_setup, "fetch_slack_channels", return_value=[
                {"id": "C123", "name": "other"}]), \
             patch.object(guided_setup, "_get_json", return_value={
                 "ok": True, "channel": {"is_channel": True, "is_private": False,
                                         "is_member": False, "name": "engineering"}}):
            errors = guided_setup.preflight(values)
        self.assertIn("not a member of #engineering", errors[0])
        self.assertNotIn("xoxb-secret", errors[0])

    def test_slack_history_scope_failure_names_permission_and_reinstall(self):
        values = {"CODEBOT_COMM_CHANNEL": "slack", "CODEBOT_SLACK_CHANNEL_ID": "C123",
                  "CODEBOT_SLACK_BOT_TOKEN": "xoxb-secret", "CODEBOT_SLACK_APP_TOKEN": "xapp-secret"}
        with patch.object(guided_setup, "fetch_slack_channels", return_value=[{"id": "C123", "name": "build"}]), \
             patch.object(guided_setup, "_get_json", return_value={"ok": False, "error": "missing_scope",
                                                                  "needed": "channels:history"}):
            errors = guided_setup.test_section("Conversation", values)
        self.assertIn("channels:history", errors[0])
        self.assertIn("reinstall", errors[0])
        self.assertNotIn("xoxb-secret", errors[0])

    def test_slack_history_read_failure_reports_membership_not_scope(self):
        values = {"CODEBOT_COMM_CHANNEL": "slack", "CODEBOT_SLACK_CHANNEL_ID": "C123",
                  "CODEBOT_SLACK_BOT_TOKEN": "xoxb-secret", "CODEBOT_SLACK_APP_TOKEN": "xapp-secret"}
        with patch.object(guided_setup, "fetch_slack_channels", return_value=[{"id": "C123", "name": "build"}]), \
             patch.object(guided_setup, "_get_json", side_effect=[
                 {"ok": False, "error": "not_in_channel"},
                 {"ok": True, "channel": {"is_channel": True, "is_member": False, "name": "build"}}]):
            errors = guided_setup.test_section("Conversation", values)
        self.assertIn("not a member", errors[0])
        self.assertNotIn("channels:history", errors[0])

    def test_loads_distinct_statuses_from_jira_project_before_mapping(self):
        values = {"CODEBOT_JIRA_URL": "https://site.atlassian.net",
                  "CODEBOT_JIRA_PROJECT_KEY": "ENG", "CODEBOT_JIRA_EMAIL": "bot@example.org",
                  "CODEBOT_JIRA_API_TOKEN": "secret-token"}
        responses = [{"accountId": "me"}, {"key": "ENG"}, [
            {"statuses": [{"name": "Ready"}, {"name": "In Progress"}]},
            {"statuses": [{"name": "ready"}, {"name": "Review"}, {"name": "Done"}]}]]
        with patch.object(guided_setup, "_get_json", side_effect=responses) as request:
            self.assertEqual(guided_setup.fetch_jira_statuses(values),
                             ["Done", "In Progress", "Ready", "Review"])
        self.assertEqual(request.call_args_list[2].args[0],
                         "https://site.atlassian.net/rest/api/3/project/ENG/statuses")
        self.assertTrue(all(call.args[1].startswith("Basic ") for call in request.call_args_list))

        with patch.object(guided_setup, "_get_json") as request:
            with self.assertRaisesRegex(RuntimeError, "CODEBOT_JIRA_API_TOKEN"):
                guided_setup.fetch_jira_statuses(values | {"CODEBOT_JIRA_API_TOKEN": ""})
            request.assert_not_called()

    def test_jira_projects_are_loaded_after_authentication_across_pages(self):
        values = {"CODEBOT_JIRA_URL": "https://site.atlassian.net",
                  "CODEBOT_JIRA_EMAIL": "bot@example.org",
                  "CODEBOT_JIRA_API_TOKEN": "secret-token"}
        pages = [{"accountId": "me"},
                 {"startAt": 0, "maxResults": 50, "isLast": False,
                  "nextPage": "https://site.atlassian.net/rest/api/3/project/search?startAt=50",
                  "values": [{"key": "OPS", "name": "Operations"}]},
                 {"startAt": 50, "maxResults": 50, "isLast": True,
                  "values": [{"key": "ENG", "name": "Engineering"}]}]
        with patch.object(guided_setup, "_get_json", side_effect=pages) as get:
            self.assertEqual(guided_setup.fetch_jira_projects(values),
                             [{"key": "ENG", "name": "Engineering"},
                              {"key": "OPS", "name": "Operations"}])
        self.assertIn("startAt=50", get.call_args_list[-1].args[0])
        self.assertTrue(all("secret-token" not in call.args[0] for call in get.call_args_list))

    def test_jira_project_search_paginates_without_next_page_link(self):
        values = {"CODEBOT_JIRA_URL": "https://site.atlassian.net", "CODEBOT_JIRA_EMAIL": "a@example.com",
                  "CODEBOT_JIRA_API_TOKEN": "secret"}
        with patch.object(guided_setup, "_get_json", side_effect=[
                {"accountId": "me"},
                {"startAt": 0, "maxResults": 1, "total": 2, "isLast": False,
                 "values": [{"key": "A", "name": "Alpha"}]},
                {"startAt": 1, "maxResults": 1, "total": 2, "isLast": True,
                 "values": [{"key": "B", "name": "Beta"}]}]) as request:
            self.assertEqual(len(guided_setup.fetch_jira_projects(values)), 2)
        self.assertIn("startAt=1", request.call_args_list[-1].args[0])

    def test_github_projects_are_discovered_with_gh_token_and_paginated(self):
        values = {"GH_TOKEN": "gh-secret", "CODEBOT_REPO_PATH": "/target/repo"}
        pages = [{"data": {"repositoryOwner": {"projectsV2": {
            "nodes": [{"title": "Roadmap", "url": "https://github.com/orgs/acme/projects/5"}],
            "pageInfo": {"hasNextPage": True, "endCursor": "cursor-2"}}}}},
                 {"data": {"repositoryOwner": {"projectsV2": {
                     "nodes": [{"title": "Backlog", "url": "https://github.com/orgs/acme/projects/2"}],
                     "pageInfo": {"hasNextPage": False}}}}}]
        origin = subprocess.CompletedProcess([], 0, "git@github-work:acme/project.git\n", "")
        with patch.object(guided_setup.subprocess, "run", return_value=origin) as git, \
             patch.object(guided_setup, "_get_json", side_effect=pages) as request:
            self.assertEqual(guided_setup.fetch_github_projects(values), [
                {"title": "Backlog", "url": "https://github.com/orgs/acme/projects/2"},
                {"title": "Roadmap", "url": "https://github.com/orgs/acme/projects/5"}])
        git.assert_called_once()
        self.assertEqual(request.call_args_list[0].kwargs["payload"]["variables"]["owner"], "acme")
        self.assertEqual(request.call_args_list[1].kwargs["payload"]["variables"]["cursor"], "cursor-2")
        self.assertTrue(all("gh-secret" not in call.args[0] for call in request.call_args_list))

    def test_github_project_errors_explain_token_scope_or_owner(self):
        values = {"GH_TOKEN": "gh-secret", "CODEBOT_GH_PROJECT_OWNER": "acme"}
        with patch.object(guided_setup, "_get_json", return_value={
                "errors": [{"message": "Resource not accessible by integration"}]}):
            with self.assertRaisesRegex(RuntimeError, "project write permission"):
                guided_setup.fetch_github_projects(values)
        self.assertEqual(guided_setup._github_project_owner({
            "CODEBOT_GH_PROJECT_URL": "https://github.com/users/somebody/projects/7"}), "somebody")
        with self.assertRaisesRegex(RuntimeError, "CODEBOT_REPO_PATH"):
            guided_setup.fetch_github_projects({"GH_TOKEN": "gh-secret"})


class LosslessEnv(unittest.TestCase):
    def test_text_menu_enables_and_disables_self_healing_without_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            original = "# keep\nUNKNOWN=untouched\nGH_TOKEN=secret\n"
            path.write_text(original)
            section = str(guided_setup.PRIMARY_PAGES.index("Self-healing") + 1)
            with patch("builtins.input", side_effect=[section, "auto", "s", section, "off", "s", "q"]), \
                 patch.object(guided_setup.subprocess, "run") as process, \
                 redirect_stdout(StringIO()) as output:
                self.assertTrue(guided_setup.text_setup(EnvFile(path)))
            process.assert_not_called()
            self.assertEqual(EnvFile(path).values["CODEBOT_SELF_HEAL"], "off")
            self.assertEqual(path.read_text(), original + "CODEBOT_SELF_HEAL=off\n")
            self.assertIn("CODEBOT_SELF_HEAL=auto", path.with_name(".env.bak.2").read_text())
            self.assertIn("Enabled", output.getvalue())
            self.assertIn("recreate", output.getvalue())
            self.assertIn("[x] Self-healing", output.getvalue())

    def test_text_self_healing_numbered_modes_and_clear_disable(self):
        for entered, expected in (("1", "off"), ("2", "observe"), ("3", "propose"), ("4", "auto"), ("/clear", "off")):
            with self.subTest(entered=entered), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / ".env"
                path.write_text("CODEBOT_SELF_HEAL=propose\n")
                with patch("builtins.input", side_effect=[entered, "s"]), redirect_stdout(StringIO()):
                    guided_setup._text_section(EnvFile(path), "Self-healing")
                self.assertEqual(EnvFile(path).values["CODEBOT_SELF_HEAL"], expected)

    def test_text_self_healing_defaults_off_and_keeps_existing_mode(self):
        for existing in (None, "observe", "propose", "auto"):
            with self.subTest(existing=existing), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / ".env"
                if existing:
                    path.write_text(f"CODEBOT_SELF_HEAL={existing}\n")
                with patch("builtins.input", side_effect=["", "s"]), redirect_stdout(StringIO()):
                    guided_setup._text_section(EnvFile(path), "Self-healing")
                self.assertEqual(EnvFile(path).values["CODEBOT_SELF_HEAL"], existing or "off")

    def test_text_self_healing_discard_does_not_enable_repairs(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            original = "CODEBOT_SELF_HEAL=off\n"
            path.write_text(original)
            with patch("builtins.input", side_effect=["auto", "d"]), redirect_stdout(StringIO()):
                guided_setup._text_section(EnvFile(path), "Self-healing")
            self.assertEqual(path.read_text(), original)
            self.assertEqual(list(path.parent.glob(".env.bak.*")), [])

    def test_preserves_unknown_comments_inactive_values_and_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            original = ("# keep this comment\nUNRECOGNIZED=legacy\n"
                        "CODEBOT_SLACK_BOT_TOKEN=secret\nCODEBOT_DOC_ID=inactive-doc\n")
            path.write_text(original)
            env = EnvFile(path)
            self.assertEqual(env.preview({"CODEBOT_SLACK_BOT_TOKEN": "new-secret"}),
                             "CODEBOT_SLACK_BOT_TOKEN: (set) -> (set)")
            with self.assertRaises(ValueError):
                env.save({"CODEBOT_SLACK_CHANNEL_ID": "invalid"})
            self.assertEqual(path.read_text(), original)
            # Save is only possible after all selected required fields are valid.
            with patch("scripts.setup_env.validate", return_value=[]):
                backup = env.save({"CODEBOT_SLACK_CHANNEL_ID": "C123"})
            self.assertEqual(backup.read_text(), original)
            self.assertIn("UNRECOGNIZED=legacy\n", path.read_text())
            self.assertIn("CODEBOT_SLACK_BOT_TOKEN=secret\n", path.read_text())
            self.assertIn("CODEBOT_DOC_ID=inactive-doc\n", path.read_text())
            self.assertIn("CODEBOT_SLACK_CHANNEL_ID=C123\n", path.read_text())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_cancel_after_valid_preview_preserves_original_bytes_and_creates_no_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "project"
            repo.mkdir()
            (repo / ".git").mkdir()
            path = root / ".env"
            original = (f"CODEBOT_REPO_PATH={repo}\nGH_TOKEN=secret\nCODEBOT_DOC_ID=doc\n"
                        "CODEBOT_USER_EMAIL=person@example.org\nCLAUDE_CODE_OAUTH_TOKEN=token\n"
                        "# keep unchanged\n")
            path.write_text(original)
            with patch("builtins.input", return_value="n"), redirect_stdout(StringIO()):
                self.assertFalse(guided_setup._apply(EnvFile(path),
                                                     {"CODEBOT_PROJECT_NAME": "renamed"}))
            self.assertEqual(path.read_text(), original)
            self.assertEqual(list(root.glob(".env.bak.*")), [])

    def test_text_mode_can_complete_first_time_setup_without_textual(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "project"
            repo.mkdir()
            (repo / ".git").mkdir()
            menu = iter(["1", "2", "q"])
            def answer(prompt):
                if "Select a section" in prompt:
                    return next(menu)
                if "Save and Close" in prompt:
                    return "s"
                if "CODEBOT_REPO_PATH" in prompt:
                    return str(repo)
                if "GH_TOKEN" in prompt:
                    return "gh-secret"
                if "CODEBOT_DOC_ID" in prompt:
                    return "doc-id"
                return ""
            output = StringIO()
            with patch.object(guided_setup, "ROOT", root), \
                 patch("builtins.input", side_effect=answer), \
                 patch.object(guided_setup, "test_section", return_value=[]), \
                 patch.object(guided_setup.sys.stdin, "isatty", return_value=False), \
                 redirect_stdout(output):
                self.assertTrue(guided_setup.text_setup(EnvFile(root / ".env")))
            saved = (root / ".env").read_text()
            self.assertIn("CODEBOT_REPO_PATH=", saved)
            self.assertIn("GH_TOKEN=gh-secret", saved)
            self.assertIn("CODEBOT_DOC_ID=doc-id", saved)
            self.assertNotIn("gh-secret", output.getvalue())
            self.assertIn("[x] Repository", output.getvalue())
            self.assertIn("[x] Backlog", output.getvalue())

    def test_text_mode_chooses_jira_statuses_from_numbered_live_options(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            menu = iter(["2", "q"])
            def answer(prompt):
                if "Select a section" in prompt:
                    return next(menu)
                if "Save and Close" in prompt:
                    return "s"
                for key, value in (("CODEBOT_TASK_SOURCE", "jira"), ("CODEBOT_JIRA_URL", "https://site.atlassian.net"),
                                   ("CODEBOT_JIRA_EMAIL", "bot@example.org"), ("CODEBOT_JIRA_API_TOKEN", "jira-secret"),
                                   ("CODEBOT_JIRA_PICK_LABEL", "codebot-ready")):
                    if key in prompt:
                        return value
                if "CODEBOT_JIRA_PROJECT_KEY" in prompt:
                    return "1"
                if "Select a status" in prompt:
                    return "1" if "none" in prompt else ""
                return ""
            with patch("builtins.input", side_effect=answer), \
                 patch.object(guided_setup.sys.stdin, "isatty", return_value=False), \
                 patch.object(guided_setup, "fetch_jira_projects",
                               return_value=[{"key": "ENG", "name": "Engineering"}]) as projects, \
                 patch.object(guided_setup, "fetch_jira_statuses",
                               return_value=["Backlog", "Building", "Done", "Review"]) as fetch, \
                 patch.object(guided_setup, "test_section", return_value=[]), \
                 redirect_stdout(StringIO()) as output:
                self.assertTrue(guided_setup.text_setup(EnvFile(root / ".env")))
            projects.assert_called_once()
            fetch.assert_called_once()
            self.assertEqual(EnvFile(root / ".env").values["CODEBOT_JIRA_PROJECT_KEY"], "ENG")
            self.assertIn("Engineering", output.getvalue())

    def test_text_mode_chooses_joined_slack_channel_after_both_tokens(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            menu = iter(["3", "q"])
            def answer(prompt):
                if "Select a section" in prompt:
                    return next(menu)
                if "Save and Close" in prompt:
                    return "s"
                for key, value in (("CODEBOT_COMM_CHANNEL", "slack"),
                                   ("CODEBOT_SLACK_BOT_TOKEN", "xoxb-token"),
                                   ("CODEBOT_SLACK_APP_TOKEN", "xapp-token")):
                    if key in prompt:
                        return value
                if "CODEBOT_SLACK_CHANNEL_ID" in prompt:
                    return "2"
                return ""
            channels = [{"id": "C1", "name": "build"}, {"id": "C2", "name": "release"}]
            with patch("builtins.input", side_effect=answer), \
                  patch.object(guided_setup.sys.stdin, "isatty", return_value=False), \
                  patch.object(guided_setup, "fetch_slack_channels", return_value=channels) as fetch, \
                  patch.object(guided_setup, "test_section", return_value=[]), \
                  redirect_stdout(StringIO()) as output:
                self.assertTrue(guided_setup.text_setup(EnvFile(root / ".env")))
            fetch.assert_called_once()
            self.assertEqual(fetch.call_args.args[0]["CODEBOT_SLACK_BOT_TOKEN"], "xoxb-token")
            self.assertEqual(fetch.call_args.args[0]["CODEBOT_SLACK_APP_TOKEN"], "xapp-token")
            self.assertEqual(EnvFile(root / ".env").values["CODEBOT_SLACK_CHANNEL_ID"], "C2")
            self.assertNotIn("xoxb-token", output.getvalue())

    def test_text_mode_chooses_github_board_from_discovered_titles(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            boards = [{"title": "Backlog", "url": "https://github.com/orgs/acme/projects/2"},
                       {"title": "Roadmap", "url": "https://github.com/orgs/acme/projects/5"}]
            (root / ".env").write_text("GH_TOKEN=gh-secret\n")
            menu = iter(["2", "q"])
            def answer(prompt):
                if "Select a section" in prompt:
                    return next(menu)
                if "Save and Close" in prompt:
                    return "s"
                if "CODEBOT_TASK_SOURCE" in prompt:
                    return "github"
                if "CODEBOT_GH_PROJECT_URL" in prompt:
                    return "2"
                return ""
            with patch("builtins.input", side_effect=answer), \
                  patch.object(guided_setup.sys.stdin, "isatty", return_value=False), \
                  patch.object(guided_setup, "fetch_github_projects", return_value=boards) as fetch, \
                  patch.object(guided_setup, "test_section", return_value=[]), \
                  redirect_stdout(StringIO()) as output:
                self.assertTrue(guided_setup.text_setup(EnvFile(root / ".env")))
            fetch.assert_called_once()
            self.assertEqual(fetch.call_args.args[0]["GH_TOKEN"], "gh-secret")
            self.assertEqual(EnvFile(root / ".env").values["CODEBOT_GH_PROJECT_URL"], boards[1]["url"])
            self.assertIn("Roadmap", output.getvalue())

    def test_text_mode_failed_save_preserves_file_and_exit_has_no_pending_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("# preserve\n")
            menu = iter(["3", "q"])
            attempts = 0
            def answer(prompt):
                nonlocal attempts
                if "Select a section" in prompt:
                    return next(menu)
                if "Save and Close" in prompt:
                    attempts += 1
                    return "s" if attempts == 1 else "d"
                if "CODEBOT_COMM_CHANNEL" in prompt:
                    return "slack"
                if "CODEBOT_SLACK_BOT_TOKEN" in prompt:
                    return "xoxb-secret"
                if "CODEBOT_SLACK_APP_TOKEN" in prompt:
                    return "xapp-secret"
                if "CODEBOT_SLACK_CHANNEL_ID" in prompt:
                    return "1"
                return ""
            with patch("builtins.input", side_effect=answer), \
                 patch.object(guided_setup.sys.stdin, "isatty", return_value=False), \
                 patch.object(guided_setup, "fetch_slack_channels", return_value=[{"id": "C1", "name": "build"}]), \
                 patch.object(guided_setup, "test_section", return_value=["Slack bot lacks channels:history"]), \
                 redirect_stdout(StringIO()) as output:
                # EOF on retry discards the unsaved draft and returns to the menu.
                self.assertTrue(guided_setup.text_setup(EnvFile(path)))
            self.assertEqual(path.read_text(), "# preserve\n")
            self.assertIn("channels:history", output.getvalue())

    def test_text_menu_full_test_reads_only_saved_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("UNKNOWN=untouched\n")
            with patch("builtins.input", side_effect=["t", "q"]), \
                 patch.object(guided_setup, "test_all", return_value=["GH_TOKEN: missing"]) as check, \
                 redirect_stdout(StringIO()) as output:
                self.assertTrue(guided_setup.text_setup(EnvFile(path)))
            check.assert_called_once_with({"UNKNOWN": "untouched"})
            self.assertIn("GH_TOKEN: missing", output.getvalue())
            self.assertEqual(path.read_text(), "UNKNOWN=untouched\n")

    def test_text_github_manual_url_when_other_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("GH_TOKEN=secret\n")
            url = "https://github.com/users/alternate/projects/4"
            choices = iter(["2", "github", "m", url, "s", "q"])
            def answer(prompt):
                if any(word in prompt for word in ("Select a section", "CODEBOT_TASK_SOURCE",
                                                    "number or m", "CODEBOT_GH_PROJECT_URL",
                                                    "Save and Close")):
                    return next(choices)
                return ""
            with patch("builtins.input", side_effect=answer), \
                 patch.object(guided_setup.sys.stdin, "isatty", return_value=False), \
                 patch.object(guided_setup, "fetch_github_projects", return_value=[
                     {"title": "Primary", "url": "https://github.com/orgs/acme/projects/1"}]), \
                 patch.object(guided_setup, "test_section", return_value=[]), \
                 redirect_stdout(StringIO()):
                self.assertTrue(guided_setup.text_setup(EnvFile(path)))
            self.assertEqual(EnvFile(path).values["CODEBOT_GH_PROJECT_URL"], url)

    def test_unavailable_tui_falls_back_to_text_without_editing_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = "# previous setup\nUNKNOWN_SECRET=leave-me-alone\n"
            (root / ".env").write_text(original)
            with patch.object(guided_setup, "ROOT", root), \
                 patch.object(guided_setup.sys, "argv", ["setup"]), \
                 patch.object(guided_setup.sys.stdin, "isatty", return_value=True), \
                 patch.object(guided_setup.sys.stdout, "isatty", return_value=True), \
                 patch.dict("os.environ", {"TERM": "xterm-256color"}), \
                 patch.object(guided_setup, "_ensure_textual", side_effect=RuntimeError("offline")), \
                 patch.object(guided_setup, "text_setup", return_value=False) as fallback, \
                 redirect_stdout(StringIO()):
                self.assertEqual(guided_setup.main(), 1)
            fallback.assert_called_once()
            self.assertEqual((root / ".env").read_text(), original)

    def test_installed_textual_relaunches_inside_venv_even_if_python_is_symlinked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            venv_python = root / ".venv/bin/python"
            venv_python.parent.mkdir(parents=True)
            venv_python.symlink_to(guided_setup.sys.executable)
            self.assertEqual(venv_python.resolve(), Path(guided_setup.sys.executable).resolve())
            with patch.object(guided_setup, "ROOT", root), \
                 patch.object(guided_setup.sys, "argv", ["setup"]), \
                 patch.object(guided_setup.sys.stdin, "isatty", return_value=True), \
                 patch.object(guided_setup.sys.stdout, "isatty", return_value=True), \
                 patch.object(guided_setup.sys, "prefix", "/usr/local/system-python"), \
                 patch.dict("os.environ", {"TERM": "xterm-256color"}), \
                 patch.object(guided_setup, "_ensure_textual", return_value=venv_python), \
                 patch.object(guided_setup.subprocess, "call", return_value=0) as relaunch:
                self.assertEqual(guided_setup.main(), 0)
            self.assertEqual(relaunch.call_args.args[0],
                             [str(venv_python), "-m", "scripts.setup"])
            self.assertEqual(relaunch.call_args.kwargs["env"]["CODEBOT_SETUP_TUI_READY"], "1")


if __name__ == "__main__":
    unittest.main()
