"""Startup behavior tests for the managed agent runtime."""
import pathlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

with patch.dict("sys.modules", {
    "gdoc_client": MagicMock(),
    "task_source": MagicMock(),
    "gmail_client": MagicMock(),
}):
    import main

# Never let a test that reaches save_state() write the real data/state.json (it would
# poison a live deployment sharing this checkout).
main.config.STATE_PATH = pathlib.Path(tempfile.mkdtemp()) / "state.json"


class RuntimeValidationTests(unittest.TestCase):
    def test_empty_wait_inbox_is_quiet_and_still_polled(self):
        state = {"state": "WAIT_STUCK", "item": "task", "thread_id": "C:1"}
        before = dict(state)
        with patch.object(main.gmail_client, "poll_reply", return_value=None) as poll, \
             patch.object(main.log, "debug") as debug:
            for _ in range(3):
                main.handle_wait(state)
        self.assertEqual(poll.call_count, 3)
        poll.assert_called_with("C:1")
        debug.assert_not_called()
        self.assertEqual(state, before)

    def test_received_reply_is_still_logged_and_queued(self):
        state = {"state": "WAIT_STUCK", "item": "task", "thread_id": "C:1"}
        with patch.object(main.gmail_client, "poll_reply", return_value=("message", "retry")), \
             patch.object(main, "_note_contact"), patch.object(main, "_credential_action", return_value=None), \
             patch.object(main, "_queue_conversation", return_value=True) as queue, \
             self.assertLogs(main.log, level="INFO") as logs:
            main.handle_wait(state)
        self.assertIn("reply received in WAIT_STUCK: 'retry'", "\n".join(logs.output))
        queue.assert_called_once_with(state, "message", "retry")

    def test_waiting_tick_is_quiet_without_changing_two_second_wait(self):
        state = {"state": "WAIT_STUCK", "item": "task", "thread_id": "C:1"}
        with patch.object(main, "load_state", return_value=state), patch.object(main, "save_state"), \
             patch.object(main, "_ensure_task_language"), patch.object(main, "_announce_state"), \
             patch.object(main, "_credential_tick"), patch.object(main, "check_commands", return_value=False), \
             patch.object(main, "_dispatch_conversation", return_value=False), \
             patch.object(main, "_dispatch_feedback", return_value=False), patch.object(main, "_receive_feedback"), \
             patch.object(main, "_maybe_ping"), patch.object(main.task_records, "apply_contacts"), \
             patch.object(main.gmail_client, "retry_deliveries"), \
             patch.object(main.gmail_client, "poll_reply", return_value=None), \
             patch.object(main.gmail_client, "wait", side_effect=SystemExit("stop")) as wait, \
             patch.object(main.config, "TARGET_MODE", "repo"), patch.object(main.config, "POLL_INTERVAL_SECONDS", 120), \
             patch.object(main.log, "debug") as debug:
            with self.assertRaisesRegex(SystemExit, "stop"):
                main._run_loop()
        wait.assert_called_once_with(2)
        debug.assert_not_called()

    def test_exhausted_network_retry_logs_pause_instead_of_another_retry(self):
        state = {"state": "EXPLORING", "item": "task", "task_language": "English"}
        with patch.object(main, "load_state", return_value=state), \
             patch.object(main, "save_state"), patch.object(main, "_ensure_task_language"), \
             patch.object(main, "_announce_state"), patch.object(main, "email"), \
             patch.object(main, "check_commands", return_value=False), \
             patch.object(main, "_dispatch_feedback", return_value=False), \
             patch.object(main, "_receive_feedback"), patch.object(main, "_maybe_ping"), \
             patch.object(main.diagnostics, "report"), patch.object(main.task_records, "apply_contacts"), \
             patch.object(main.gmail_client, "retry_deliveries"), \
             patch.object(main.gmail_client, "wait", side_effect=SystemExit("stop")) as wait, \
             patch.object(main.config, "MAX_STATE_FAILURES", 1), \
             patch.dict(main.PHASES, {"EXPLORING": MagicMock(side_effect=ConnectionError("fetch failed"))}), \
             self.assertLogs(main.log, level="WARNING") as logs:
            with self.assertRaisesRegex(SystemExit, "stop"):
                main._run_loop()
        self.assertEqual(state["state"], "WAIT_STUCK")
        self.assertIn("automatic retries paused", "\n".join(logs.output))
        self.assertNotIn("retrying in", "\n".join(logs.output))
        wait.assert_called_once_with(main.config.POLL_INTERVAL_SECONDS)

    def test_network_retry_delays_start_at_one_and_survive_restart(self):
        state = {"state": "EXPLORING", "item": "task", "task_language": "English"}
        clock = [100.0]
        delays = []
        def wait(seconds):
            delays.append(seconds)
            clock[0] += seconds
        def run_failures(count):
            with patch.object(main, "load_state", side_effect=lambda: dict(state)), \
                 patch.object(main, "save_state", side_effect=lambda current: state.update(current)), \
                 patch.object(main, "_ensure_task_language"), patch.object(main, "_announce_state"), \
                 patch.object(main, "check_commands", return_value=False), \
                 patch.object(main, "_dispatch_feedback", return_value=False), \
                 patch.object(main, "_receive_feedback"), patch.object(main, "_maybe_ping"), \
                 patch.object(main.diagnostics, "report"), patch.object(main.task_records, "apply_contacts"), \
                 patch.object(main.gmail_client, "retry_deliveries"), \
                 patch.object(main.gmail_client, "wait", side_effect=wait), \
                 patch.object(main.time, "time", side_effect=lambda: clock[0]), \
                 patch.object(main.config, "MAX_STATE_FAILURES", 10), \
                 patch.object(main.config, "POLL_INTERVAL_SECONDS", 60), \
                 patch.dict(main.PHASES, {"EXPLORING": MagicMock(side_effect=[
                     *[ConnectionError("fetch failed") for _ in range(count)], SystemExit("stop")])}):
                with self.assertRaisesRegex(SystemExit, "stop"):
                    main._run_loop()
        run_failures(2)
        self.assertEqual(delays, [1, 2])
        self.assertEqual(state["technical_retry"]["attempts"], 2)
        # Restart before the persisted retry time; ordinary polling must not
        # turn the remaining one second into a sixty-second wait.
        clock[0] -= 1
        run_failures(2)
        self.assertEqual(delays, [1, 2, 1, 4, 8])

    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        self.superpowers = root / "superpowers"
        self.bridge = root / "bridge"
        self.openspec = root / "openspec-skills"
        for path in (
            self.openspec / ".claude-plugin/plugin.json",
            self.openspec / "skills/openspec-explore/SKILL.md",
            self.openspec / "opencode/openspec-explore/SKILL.md",
            self.superpowers / ".claude-plugin/plugin.json",
            self.superpowers / "skills/using-superpowers/SKILL.md",
            self.superpowers / ".opencode/plugins/superpowers.js",
            self.bridge / ".claude-plugin/plugin.json",
            self.bridge / "skills/coderbot-openspec-workflow/SKILL.md",
            self.bridge / "package.json",
            self.bridge / ".opencode/plugins/coderbot-openspec.js",
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fixture")
        self.path_patches = (
            patch.object(main.config, "SUPERPOWERS_PLUGIN_DIR", self.superpowers, create=True),
            patch.object(main.config, "BRIDGE_PLUGIN_DIR", self.bridge, create=True),
            patch.object(main.config, "OPENSPEC_SKILLS_DIR", self.openspec, create=True),
        )
        for path_patch in self.path_patches:
            path_patch.start()

    def tearDown(self):
        for path_patch in reversed(self.path_patches):
            path_patch.stop()
        self.temporary_directory.cleanup()

    def validate(self):
        self.assertTrue(hasattr(main, "validate_managed_runtime"),
                        "managed runtime validator is missing")
        main.validate_managed_runtime()

    def test_fixture_runtime_is_valid_for_each_agent(self):
        for agent in ("claude", "opencode"):
            with self.subTest(agent=agent), patch.object(main.config, "AGENT", agent):
                self.validate()

    def test_each_missing_claude_component_fails_clearly(self):
        components = {
            "Superpowers Claude manifest": self.superpowers / ".claude-plugin/plugin.json",
            "Superpowers skill tree": self.superpowers / "skills/using-superpowers/SKILL.md",
            "bridge manifest": self.bridge / ".claude-plugin/plugin.json",
            "bridge skill": self.bridge / "skills/coderbot-openspec-workflow/SKILL.md",
            "OpenSpec skills manifest": self.openspec / ".claude-plugin/plugin.json",
            "OpenSpec Claude skills": self.openspec / "skills/openspec-explore/SKILL.md",
        }
        with patch.object(main.config, "AGENT", "claude"):
            for label, path in components.items():
                with self.subTest(component=label):
                    contents = path.read_text()
                    path.unlink()
                    try:
                        with self.assertRaisesRegex(SystemExit, label):
                            self.validate()
                    finally:
                        path.write_text(contents)

    def test_each_missing_opencode_component_fails_clearly(self):
        components = {
            "Superpowers OpenCode entrypoint": self.superpowers / ".opencode/plugins/superpowers.js",
            "Superpowers skill tree": self.superpowers / "skills/using-superpowers/SKILL.md",
            "bridge manifest": self.bridge / ".claude-plugin/plugin.json",
            "bridge skill": self.bridge / "skills/coderbot-openspec-workflow/SKILL.md",
            "bridge OpenCode package": self.bridge / "package.json",
            "bridge OpenCode entrypoint": self.bridge / ".opencode/plugins/coderbot-openspec.js",
            "OpenSpec OpenCode skills": self.openspec / "opencode/openspec-explore/SKILL.md",
        }
        with patch.object(main.config, "AGENT", "opencode"):
            for label, path in components.items():
                with self.subTest(component=label):
                    contents = path.read_text()
                    path.unlink()
                    try:
                        with self.assertRaisesRegex(SystemExit, label):
                            self.validate()
                    finally:
                        path.write_text(contents)

    def _main_env(self, **overrides):
        values = {"AGENT": "claude", "USER_EMAIL": "user@example.test", "DOC_ID": "",
                  "TASK_SOURCE": "gdoc", "GH_PROJECT_OWNER": "", "GH_PROJECT_NUMBER": 0}
        values.update(overrides)
        return [patch.object(main.config, key, value, create=True) for key, value in values.items()]

    def _run_main(self, patches):
        with patch.object(main, "validate_managed_runtime", create=True), \
             patch.object(main.task_source, "SOURCES", ("gdoc", "github"), create=True), \
             patch.object(main, "_acquire_single_instance_lock", create=True), \
             patch.object(main.threading.Thread, "start"), \
             patch.object(main, "load_state", return_value={"state": "IDLE"}), \
             patch.object(main, "check_commands", return_value=False), \
             patch.object(main.config, "REPO_PATH", Path(self.temporary_directory.name)), \
             patch.object(Path, "exists", return_value=True), \
             patch.dict(main.PHASES, {"IDLE": MagicMock(side_effect=SystemExit("tick reached"))}):
            for p in patches:
                p.start()
            try:
                main.main()
            finally:
                for p in patches:
                    p.stop()

    def test_gdoc_source_still_requires_doc_id(self):
        with self.assertRaisesRegex(SystemExit, "CODEBOT_DOC_ID"):
            self._run_main(self._main_env())

    def test_github_source_requires_project_and_not_doc_id(self):
        with self.assertRaisesRegex(SystemExit, "CODEBOT_GH_PROJECT_URL"):
            self._run_main(self._main_env(TASK_SOURCE="github"))
        with patch.object(main.task_source, "validate"), \
             patch.object(main.task_source, "describe", return_value="project=acme/1"):
            with self.assertRaisesRegex(SystemExit, "tick reached"):
                self._run_main(self._main_env(TASK_SOURCE="github", GH_PROJECT_OWNER="acme",
                                              GH_PROJECT_NUMBER=1))

    def test_unknown_source_and_backend_validation_failures_stop_startup(self):
        with self.assertRaisesRegex(SystemExit, "CODEBOT_TASK_SOURCE"):
            self._run_main(self._main_env(TASK_SOURCE="jira"))
        with patch.object(main.task_source, "validate", side_effect=RuntimeError("no project scope")):
            with self.assertRaisesRegex(SystemExit, "no project scope"):
                self._run_main(self._main_env(DOC_ID="doc"))

    def test_main_loop_does_not_count_transient_network_errors_as_state_failures(self):
        import ssl
        state = {"state": "WAIT_REPLY", "item": "task", "thread_id": "t1",
                 "task_language": "English"}
        patches = self._main_env(DOC_ID="doc") + [
            patch.object(main.task_source, "validate"),
            patch.object(main.task_source, "describe", return_value="doc"),
            patch.object(main, "load_state", return_value=state),
            patch.object(main, "save_state"),
            patch.object(main.diagnostics, "report"),
            patch.object(main.time, "sleep"),
            patch.object(main, "handle_wait", side_effect=[
                ssl.SSLEOFError("EOF occurred in violation of protocol"),
                KeyError("real bug"), SystemExit("tick reached")]),
        ]
        with self.assertLogs(main.log, level="WARNING") as logs:
            with self.assertRaisesRegex(SystemExit, "tick reached"):
                self._run_main(patches)
        # The TLS blip left the counter alone; the genuine exception advanced it to 1.
        self.assertEqual(state["failures"]["WAIT_REPLY"], 1)
        transient = [r for r in logs.records if "transient network error" in r.getMessage()]
        self.assertEqual(len(transient), 1)
        self.assertEqual(transient[0].levelname, "WARNING")
        self.assertIsNone(transient[0].exc_info)

    def test_main_validates_runtime_before_backlog_selection(self):
        backlog_selection = MagicMock(side_effect=SystemExit("backlog selection reached"))
        with patch.object(main.config, "AGENT", "claude"), \
             patch.object(main.config, "USER_EMAIL", "user@example.test"), \
             patch.object(main.config, "DOC_ID", "doc"), \
             patch.object(main.config, "REPO_PATH", Path(self.temporary_directory.name)), \
             patch.object(Path, "exists", return_value=True), \
             patch.object(main, "validate_managed_runtime",
                          side_effect=SystemExit("runtime unavailable"), create=True), \
             patch.object(main, "load_state", return_value={"state": "IDLE"}), \
             patch.object(main, "check_commands", return_value=False), \
             patch.dict(main.PHASES, {"IDLE": backlog_selection}):
            with self.assertRaisesRegex(SystemExit, "runtime unavailable"):
                main.main()
        backlog_selection.assert_not_called()


if __name__ == "__main__":
    unittest.main()
