"""Restart-safe archival and native pull-request lifecycle tests."""
import json
import subprocess
import pathlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

with patch.dict(sys.modules, {"gdoc_client": Mock(), "task_source": Mock(), "gmail_client": Mock()}):
    import main

# Never let a test that reaches save_state() write the real data/state.json (it would
# poison a live deployment sharing this checkout).
main.config.STATE_PATH = pathlib.Path(tempfile.mkdtemp()) / "state.json"


class ArchivalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        (self.repo / "openspec" / "changes" / "api-version").mkdir(parents=True)
        self.state = {
            "state": "ARCHIVING",
            "slug": "api-version",
            "branch": "codebot-api-version",
            "item": "Support another API version",
            "session_id": "session-1",
        }

    def tearDown(self):
        self.tmp.cleanup()

    def completed(self, args, stdout=""):
        return subprocess.CompletedProcess(args, 0, stdout, "")

    def report(self, *items, returncode=0):
        """A `openspec validate --archived --json` report; defaults to our change passing."""
        if not items:
            items = ({"id": Path(self.state["archive_path"]).name, "valid": True, "issues": []},)
        return subprocess.CompletedProcess(
            [], returncode, json.dumps({"items": list(items)}), "")

    def ok(self, command, **kwargs):
        """subprocess.run stub: archive moves the change; validate reports success."""
        if command[:2] == ["openspec", "archive"]:
            archive = self.repo / self.state["archive_path"]
            archive.parent.mkdir(parents=True, exist_ok=True)
            (self.repo / "openspec" / "changes" / "api-version").rename(archive)
        if "--json" in command and "--archived" in command:
            return self.report()
        return self.completed(command, "{}")

    def test_archive_success_validates_stages_only_openspec_and_commits(self):
        self.state["archive_error"] = "previous failure"

        with patch.object(main.config, "REPO_PATH", self.repo), \
             patch.object(main, "save_state"), \
             patch.object(main.operations, "tool", side_effect=self.ok) as process, \
             patch.object(main, "git",
                          side_effect=["", "", "?? openspec/specs/api/spec.md", "", ""]) as git:
            main.do_archive(self.state)

        commands = [c.args[0] for c in process.call_args_list]
        self.assertIn(["openspec", "archive", "api-version", "-y", "--json"], commands)
        self.assertIn(
            ["openspec", "validate", "--specs", "--strict", "--no-interactive"], commands)
        self.assertIn(
            ["openspec", "validate", "--archived", "--strict", "--no-interactive", "--json"],
            commands)
        expected_archive = self.state["archive_path"]
        self.assertIn(call(
            "add", "-A", "--", "openspec/specs",
            expected_archive), git.call_args_list)
        self.assertIn(call(
            "commit", "-m", "chore: archive OpenSpec change", "--", "openspec/specs",
            expected_archive), git.call_args_list)
        self.assertNotIn(call("add", "--", "openspec/"), git.call_args_list)
        self.assertEqual(self.state["state"], "OPEN_PR")
        self.assertNotIn("archive_error", self.state)
        self.assertRegex(self.state["archive_path"],
                         r"^openspec/changes/archive/\d{4}-\d{2}-\d{2}-api-version$")

    def test_archive_cli_and_validation_failures_stay_before_open_pr(self):
        with patch.object(main.config, "REPO_PATH", self.repo), \
             patch.object(main, "save_state"), patch.object(main, "git", return_value=""), \
             patch.object(main.operations, "tool", return_value=subprocess.CompletedProcess(
                 [], 1, "", "archive failed")):
            main.do_archive(self.state)

        self.assertEqual(self.state["state"], "ARCHIVING")
        self.assertEqual(self.state["archive_round"], 1)
        self.assertIn("archive failed", self.state["archive_error"])

    def test_resuming_committed_archive_preserves_recovery_note_and_finishes(self):
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        for key, value in (("user.name", "Fixture"), ("user.email", "fixture@example.test")):
            subprocess.run(["git", "config", key, value], cwd=self.repo, check=True)
        active = self.repo / "openspec/changes/api-version"
        (active / "proposal.md").write_text("Approved proposal")
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-qm", "active change"], cwd=self.repo, check=True)
        self.state["archive_path"] = "openspec/changes/archive/2026-10-07-api-version"
        target = self.repo / self.state["archive_path"]
        target.parent.mkdir(parents=True)
        active.rename(target)
        note = target / main.TRANSCRIPT_NAME
        note.write_text("Preserved recovery note")
        subprocess.run(["git", "add", "-A"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-qm", "archive already committed"], cwd=self.repo, check=True)
        # An extra archive-owned file requires a closing commit; active path no
        # longer exists either in the filesystem or Git's index.
        (target / "design.md").write_text("Recovered approved design")
        source = self.repo / ".git/transcript.txt"
        source.write_text("Growing recovery transcript" * 100)
        with patch.object(main.config, "REPO_PATH", self.repo), \
             patch.object(main, "_transcript_path", return_value=source), \
             patch.object(main, "_run_checked", return_value="valid") as commands, \
             patch.object(main, "_validate_archived_change"), \
             patch.object(main, "save_state"), patch.object(main, "trail"):
            main.do_archive(self.state)
            first_head = main.git("rev-parse", "HEAD")
            self.state["state"] = "ARCHIVING"
            source.write_text("Even more recovery activity")
            main.do_archive(self.state)
            self.assertEqual(first_head, main.git("rev-parse", "HEAD"))
            self.assertEqual(main.git("status", "--porcelain"), "")
        self.assertEqual(self.state["state"], "OPEN_PR")
        self.assertEqual(note.read_text(), "Preserved recovery note")
        self.assertFalse(active.exists())
        self.assertFalse(any(call.args[0][:2] == ["openspec", "archive"] for call in commands.call_args_list))

    def test_archive_commit_paths_keep_deleted_active_files_only_when_pending(self):
        self.state["archive_path"] = "openspec/changes/archive/2026-10-07-api-version"
        (self.repo / "openspec/changes/api-version").rmdir()
        with patch.object(main.config, "REPO_PATH", self.repo):
            roots = main._archive_commit_paths(self.state,
                "D openspec/changes/api-version/proposal.md\n"
                "?? openspec/changes/archive/2026-10-07-api-version/proposal.md")
            self.assertIn("openspec/changes/api-version", roots)
            roots = main._archive_commit_paths(self.state,
                " M openspec/changes/archive/2026-10-07-api-version/agent-log.md")
            self.assertNotIn("openspec/changes/api-version", roots)

    def test_strict_validation_failure_stays_before_open_pr(self):
        def run(command, **kwargs):
            if command[:2] == ["openspec", "archive"]:
                target = self.repo / self.state["archive_path"]
                target.parent.mkdir(parents=True, exist_ok=True)
                (self.repo / "openspec/changes/api-version").rename(target)
                return self.completed(command, "{}")
            return subprocess.CompletedProcess(command, 1, "", "invalid specs")

        with patch.object(main.config, "REPO_PATH", self.repo), \
             patch.object(main, "save_state"), patch.object(main, "git", return_value=""), \
             patch.object(main.operations, "tool", side_effect=run):
            main.do_archive(self.state)

        self.assertEqual(self.state["state"], "ARCHIVING")
        self.assertEqual(self.state["archive_round"], 1)

    def test_stale_sibling_archive_failure_does_not_block_our_archive(self):
        def run(command, **kwargs):
            if "--archived" in command:
                return self.report(
                    {"id": "2026-07-15-codebot-resilience", "valid": False,
                     "issues": [{"level": "ERROR", "path": "tasks.md",
                                 "message": "2 incomplete tasks (23/25 completed)"}]},
                    {"id": Path(self.state["archive_path"]).name, "valid": True,
                     "issues": []},
                    returncode=1)
            return self.ok(command)

        with patch.object(main.config, "REPO_PATH", self.repo), \
             patch.object(main, "save_state"), patch.object(main, "git", return_value=""), \
             patch.object(main.operations, "tool", side_effect=run), \
             self.assertLogs(main.log, level="WARNING") as logs:
            main.do_archive(self.state)

        self.assertEqual(self.state["state"], "OPEN_PR")
        self.assertNotIn("archive_error", self.state)
        self.assertTrue(any("2026-07-15-codebot-resilience" in line for line in logs.output))

    def test_our_archived_change_failing_strict_validation_blocks(self):
        def run(command, **kwargs):
            if "--archived" in command:
                return self.report(
                    {"id": "2026-07-15-codebot-resilience", "valid": True, "issues": []},
                    {"id": Path(self.state["archive_path"]).name, "valid": False,
                     "issues": [{"level": "ERROR", "path": "tasks.md",
                                 "message": "1 incomplete task (4/5 completed)"}]},
                    returncode=1)
            return self.ok(command)

        with patch.object(main.config, "REPO_PATH", self.repo), \
             patch.object(main, "save_state"), patch.object(main, "git", return_value=""), \
             patch.object(main.operations, "tool", side_effect=run):
            main.do_archive(self.state)

        self.assertEqual(self.state["state"], "ARCHIVING")
        self.assertEqual(self.state["archive_round"], 1)
        self.assertIn("1 incomplete task", self.state["archive_error"])
        self.assertIn(Path(self.state["archive_path"]).name, self.state["archive_error"])

    def test_archived_report_missing_our_change_or_unreadable_blocks(self):
        for stdout in ("{}", "not json", json.dumps({"items": [{"id": "other", "valid": True}]})):
            with self.subTest(stdout=stdout):
                self.setUp()

                def run(command, **kwargs):
                    if "--archived" in command:
                        return subprocess.CompletedProcess(command, 0, stdout, "")
                    return self.ok(command)

                with patch.object(main.config, "REPO_PATH", self.repo), \
                     patch.object(main, "save_state"), \
                     patch.object(main, "git", return_value=""), \
                     patch.object(main.operations, "tool", side_effect=run):
                    main.do_archive(self.state)

                self.assertEqual(self.state["state"], "ARCHIVING")
                self.assertEqual(self.state["archive_round"], 1)

    def test_stripped_first_status_line_is_still_recognized_as_archive_output(self):
        # git() strips its output, so the first porcelain line loses its leading status
        # column (" D path" -> "D path"). That must not read as an unrelated change.
        self.state["archive_path"] = "openspec/changes/archive/2026-09-02-api-version"
        status = (
            "D openspec/changes/api-version/proposal.md\n"
            " D openspec/changes/api-version/tasks.md\n"
            "?? openspec/changes/archive/2026-09-02-api-version/proposal.md\n"
            "?? openspec/specs/api/spec.md"
        )
        self.assertEqual(main._unrelated_archive_changes(self.state, status), [])
        self.assertEqual(
            main._unrelated_archive_changes(self.state, status + "\n M src/app.py"),
            ["src/app.py"])

    def test_unrelated_archive_change_is_named_in_error(self):
        def run(command, **kwargs):
            return self.ok(command)

        with patch.object(main.config, "REPO_PATH", self.repo), \
             patch.object(main, "save_state"), \
             patch.object(main.operations, "tool", side_effect=run), \
             patch.object(main, "git", side_effect=["", "", "M src/app.py"]):
            main.do_archive(self.state)

        self.assertEqual(self.state["state"], "ARCHIVING")
        self.assertIn("src/app.py", self.state["archive_error"])

    def test_archive_command_success_without_expected_target_does_not_advance(self):
        def run(command, **kwargs):  # archive "succeeds" but moves nothing
            return self.report() if "--archived" in command else self.completed(command, "{}")

        with patch.object(main.config, "REPO_PATH", self.repo), \
             patch.object(main, "save_state"), \
             patch.object(main, "git", return_value=""), \
             patch.object(main.operations, "tool", side_effect=run):
            main.do_archive(self.state)

        self.assertEqual(self.state["state"], "ARCHIVING")
        self.assertEqual(self.state["archive_round"], 1)

    def test_restart_with_existing_moved_archive_validates_without_archiving(self):
        archive = self.repo / "openspec/changes/archive/2026-09-01-api-version"
        archive.parent.mkdir(parents=True)
        (self.repo / "openspec/changes/api-version").rename(archive)
        self.state["archive_path"] = "openspec/changes/archive/2026-09-01-api-version"

        with patch.object(main.config, "REPO_PATH", self.repo), \
             patch.object(main, "save_state"), \
             patch.object(main.operations, "tool", side_effect=self.ok) as process, \
             patch.object(main, "git",
                          side_effect=[" M openspec/specs/api/spec.md", "", ""]) as git:
            main.do_archive(self.state)

        self.assertFalse(any(c.args[0][:2] == ["openspec", "archive"]
                             for c in process.call_args_list))
        self.assertIn(call(
            "add", "-A", "--", "openspec/specs",
            "openspec/changes/archive/2026-09-01-api-version"), git.call_args_list)
        self.assertEqual(self.state["state"], "OPEN_PR")

    def test_restart_after_committed_archive_advances_without_second_commit(self):
        archive = self.repo / "openspec/changes/archive/2026-09-01-api-version"
        archive.mkdir(parents=True)
        (self.repo / "openspec/changes/api-version").rmdir()
        self.state["archive_path"] = "openspec/changes/archive/2026-09-01-api-version"

        with patch.object(main.config, "REPO_PATH", self.repo), \
             patch.object(main, "save_state"), \
             patch.object(main.operations, "tool", side_effect=self.ok) as process, \
             patch.object(main, "git", return_value="") as git:
            main.do_archive(self.state)

        self.assertFalse(any(c.args[0][:2] == ["openspec", "archive"]
                             for c in process.call_args_list))
        self.assertFalse(any(c.args[:2] == ("commit", "-m") for c in git.call_args_list))
        self.assertEqual(self.state["state"], "OPEN_PR")

    def test_dirty_tracked_tree_blocks_first_archive(self):
        with patch.object(main.config, "REPO_PATH", self.repo), \
             patch.object(main, "save_state"), \
             patch.object(main, "git", return_value=" M app.py"), \
             patch.object(main.recovery, "begin", side_effect=lambda db, state, repo, reason, resume: state.update(state="RECOVERING")) as recover, \
             patch.object(main.operations, "tool") as process:
            main.do_archive(self.state)

        process.assert_not_called()
        self.assertEqual(self.state["state"], "RECOVERING")
        recover.assert_called_once()

    def test_untracked_openspec_file_blocks_first_archive(self):
        def git(*args):
            if args == ("status", "--porcelain", "--untracked-files=no"):
                return ""
            if args == ("status", "--porcelain", "--untracked-files=all", "--", "openspec/"):
                return "?? openspec/notes.txt"
            return ""

        with patch.object(main.config, "REPO_PATH", self.repo), \
             patch.object(main, "save_state"), patch.object(main, "git", side_effect=git) as git_call, \
             patch.object(main.operations, "tool") as process:
            main.do_archive(self.state)

        self.assertIn(
            call("status", "--porcelain", "--untracked-files=all", "--", "openspec/"),
            git_call.call_args_list)
        process.assert_not_called()
        self.assertEqual(self.state["state"], "ARCHIVING")

    def test_unrelated_untracked_openspec_file_is_not_staged_after_archive(self):
        def run(command, **kwargs):
            if command[:2] == ["openspec", "archive"]:
                target = self.repo / self.state["archive_path"]
                target.parent.mkdir(parents=True, exist_ok=True)
                (self.repo / "openspec/changes/api-version").rename(target)
            if "--archived" in command:
                return self.report()
            return self.completed(command, "{}")

        def git(*args):
            if args == ("status", "--porcelain", "--untracked-files=all"):
                return "?? openspec/notes.txt"
            return ""

        with patch.object(main.config, "REPO_PATH", self.repo), \
             patch.object(main, "save_state"), \
             patch.object(main.operations, "tool", side_effect=run), \
             patch.object(main, "git", side_effect=git) as git_call:
            main.do_archive(self.state)

        self.assertFalse(any(c.args and c.args[0] == "add" for c in git_call.call_args_list))
        self.assertEqual(self.state["state"], "ARCHIVING")

    def test_archive_retry_exhaustion_emails_and_waits(self):
        with patch.object(main.config, "REPO_PATH", self.repo), \
             patch.object(main, "save_state"), \
             patch.object(main.config, "ARCHIVE_MAX_ROUNDS", 2, create=True), \
             patch.object(main, "git", return_value=" M app.py"), \
             patch.object(main, "email") as email:
            main.do_archive(self.state)
            main.do_archive(self.state)

        self.assertEqual(self.state["state"], "WAIT_REPLY")
        self.assertEqual(self.state["return_state"], "ARCHIVING")
        email.assert_called_once()

    def test_archive_reply_resumes_agent_with_error_and_guidance_then_retries(self):
        state = dict(
            self.state, state="WAIT_REPLY", return_state="ARCHIVING", archive_round=3,
            archive_error="strict validation failed")
        fixed = Mock(session_id="session-2", output="committed planning fix",
                     question=None, attachments=[])
        with patch.object(main.agent_runner, "run", return_value=Mock(output='{"action":"answer"}')), \
             patch.object(main.agent_runner, "resume", return_value=fixed) as resume, \
             patch.object(main, "email"), \
             patch.object(main, "_reset_to_base_branch") as reset:
            main._handle_reply(state, "fix the malformed requirement")

        prompt = resume.call_args.args[1]
        self.assertIn("strict validation failed", prompt)
        self.assertIn("fix the malformed requirement", prompt)
        self.assertEqual(state["state"], "ARCHIVING")
        self.assertEqual(state.get("archive_round", 0), 0)
        self.assertEqual(state["session_id"], "session-2")
        reset.assert_not_called()

    def test_archive_repair_can_request_more_user_input(self):
        state = dict(
            self.state, state="WAIT_REPLY", return_state="ARCHIVING", archive_round=3,
            archive_error="strict validation failed")
        question = Mock(session_id="session-2", output="question", question="Which spec?",
                        attachments=[])
        with patch.object(main.agent_runner, "run", return_value=Mock(output='{"action":"answer"}')), \
             patch.object(main.agent_runner, "resume", return_value=question), \
             patch.object(main, "email"):
            main._handle_reply(state, "please repair it")

        self.assertEqual(state["state"], "WAIT_REPLY")
        self.assertEqual(state["return_state"], "ARCHIVING")

    def test_archive_commit_failure_uses_bounded_recovery(self):
        archive = self.repo / "openspec/changes/archive/2026-09-01-api-version"
        archive.parent.mkdir(parents=True)
        (self.repo / "openspec/changes/api-version").rename(archive)
        self.state["archive_path"] = "openspec/changes/archive/2026-09-01-api-version"
        with patch.object(main.config, "REPO_PATH", self.repo), \
             patch.object(main.operations, "tool", side_effect=self.ok), \
             patch.object(main, "git", side_effect=[" M openspec/specs/api/spec.md", "",
                                                     RuntimeError("commit failed")]):
            main.do_archive(self.state)

        self.assertEqual(self.state["state"], "ARCHIVING")
        self.assertEqual(self.state["archive_round"], 1)

    def test_date_prefixed_slug_is_not_prefixed_again_and_is_persisted_first(self):
        state = dict(self.state, slug="2026-08-31-api-version")
        old_active = self.repo / "openspec/changes/api-version"
        active = self.repo / "openspec/changes/2026-08-31-api-version"
        old_active.rename(active)

        def run(command, **kwargs):
            if command[:2] == ["openspec", "archive"]:
                target = self.repo / state["archive_path"]
                target.parent.mkdir(parents=True, exist_ok=True)
                active.rename(target)
            if "--archived" in command:
                return subprocess.CompletedProcess(command, 0, json.dumps({"items": [
                    {"id": Path(state["archive_path"]).name, "valid": True,
                     "issues": []}]}), "")
            return self.completed(command, "{}")

        with patch.object(main.config, "REPO_PATH", self.repo), \
             patch.object(main, "save_state") as save, \
             patch.object(main.operations, "tool", side_effect=run), \
             patch.object(main, "git", side_effect=["", "", ""]):
            main.do_archive(state)

        self.assertEqual(
            state["archive_path"], "openspec/changes/archive/2026-08-31-api-version")
        save.assert_called_once_with(state)

    def test_malicious_or_malformed_slugs_are_rejected_before_side_effects(self):
        invalid = (
            "../escape", "/tmp/escape", "api/version", "API-version", "api_version",
            ".", "api--version", "-api", "api-", "2026-13-40-api-version",
            "2026-09-01",
        )
        for slug in invalid:
            with self.subTest(slug=slug):
                state = dict(self.state, slug=slug)
                with patch.object(main.config, "REPO_PATH", self.repo), \
                     patch.object(main, "save_state") as save, \
                     patch.object(main.operations, "tool") as process, \
                     patch.object(main, "git") as git:
                    main.do_archive(state)
                save.assert_not_called()
                process.assert_not_called()
                git.assert_not_called()
                self.assertEqual(state["state"], "ARCHIVING")

    def test_untrusted_persisted_archive_paths_are_rejected_before_side_effects(self):
        invalid = (
            "/tmp/2026-09-01-api-version",
            "../openspec/changes/archive/2026-09-01-api-version",
            "openspec/changes/archive/../../outside/2026-09-01-api-version",
            "openspec/changes/archive/nested/2026-09-01-api-version",
            "openspec/changes/archive/2026-09-01-other-change",
            "openspec/changes/archive/not-dated-api-version",
        )
        for archive_path in invalid:
            with self.subTest(archive_path=archive_path):
                state = dict(self.state, archive_path=archive_path)
                with patch.object(main.config, "REPO_PATH", self.repo), \
                     patch.object(main, "save_state") as save, \
                     patch.object(main.operations, "tool") as process, \
                     patch.object(main, "git") as git:
                    main.do_archive(state)
                save.assert_not_called()
                process.assert_not_called()
                git.assert_not_called()
                self.assertEqual(state["state"], "ARCHIVING")

    def test_archive_symlink_escape_is_rejected_before_cli_or_git(self):
        outside = self.repo / "outside"
        outside.mkdir()
        archive_root = self.repo / "openspec/changes/archive"
        archive_root.symlink_to(outside, target_is_directory=True)
        self.state["archive_path"] = (
            "openspec/changes/archive/2026-09-01-api-version")

        with patch.object(main.config, "REPO_PATH", self.repo), \
             patch.object(main, "save_state") as save, \
             patch.object(main.operations, "tool") as process, \
             patch.object(main, "git") as git:
            main.do_archive(self.state)

        save.assert_not_called()
        process.assert_not_called()
        git.assert_not_called()
        self.assertEqual(self.state["state"], "ARCHIVING")

    def test_ambiguous_existing_archives_are_rejected_without_cli_or_git(self):
        (self.repo / "openspec/changes/api-version").rmdir()
        archive_root = self.repo / "openspec/changes/archive"
        (archive_root / "2026-08-31-api-version").mkdir(parents=True)
        (archive_root / "2026-09-01-api-version").mkdir()

        with patch.object(main.config, "REPO_PATH", self.repo), \
             patch.object(main, "save_state") as save, \
             patch.object(main.operations, "tool") as process, \
             patch.object(main, "git") as git:
            main.do_archive(self.state)

        save.assert_not_called()
        process.assert_not_called()
        git.assert_not_called()
        self.assertEqual(self.state["state"], "ARCHIVING")


class NativePullRequestTests(unittest.TestCase):
    def setUp(self):
        for helper in ("_publish_existing_pr", "_verify_pr_publication"):
            patcher = patch.object(main, helper)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        (self.repo / "openspec/changes/archive/2026-09-01-api-version").mkdir(parents=True)
        self.repo_patch = patch.object(main.config, "REPO_PATH", self.repo)
        self.repo_patch.start()

    def tearDown(self):
        self.repo_patch.stop()
        self.tmp.cleanup()

    def ready_git(self, *args):
        if args[0] == "status":
            return ""
        if args[0] == "ls-tree":
            return "openspec/changes/archive/2026-09-01-api-version/proposal.md"
        return ""

    def state(self):
        return {
            "state": "OPEN_PR",
            "slug": "api-version",
            "branch": "codebot-api-version",
            "item": "Support another API version",
            "archive_path": "openspec/changes/archive/2026-09-01-api-version",
            "has_code_review": True,
        }

    def test_native_pr_pushes_branch_and_stores_https_url(self):
        state = self.state()
        responses = [
            subprocess.CompletedProcess([], 0, "[]\n", ""),
            subprocess.CompletedProcess([], 0, "https://github.com/acme/project/pull/42\n", ""),
        ]
        with patch.object(main, "git", side_effect=self.ready_git) as git, \
             patch.object(main.operations, "tool", side_effect=responses) as process, \
             patch.object(main, "_enter_review_wait") as enter_wait:
            main.do_open_pr(state)

        self.assertIn(call("push", "-u", "origin", "codebot-api-version"), git.call_args_list)
        lookup = process.call_args_list[0].args[0]
        self.assertEqual(lookup, ["gh", "pr", "list", "--state", "open", "--head",
                                  "codebot-api-version", "--json", "url,state", "--limit", "1"])
        command = process.call_args_list[1].args[0]
        self.assertEqual(command[:4], ["gh", "pr", "create", "--base"])
        self.assertEqual(command[4], main.config.BASE_BRANCH)
        self.assertEqual(command[command.index("--head") + 1], "codebot-api-version")
        self.assertEqual(command[command.index("--title") + 1], "Support another API version")
        body = command[command.index("--body") + 1]
        self.assertIn("openspec/changes/archive/2026-09-01-api-version", body)
        self.assertEqual(state["pr_url"], "https://github.com/acme/project/pull/42")
        enter_wait.assert_called_once_with(state)

    def test_native_pr_rejects_non_https_output(self):
        state = self.state()
        responses = [
            subprocess.CompletedProcess([], 0, "[]", ""),
            subprocess.CompletedProcess([], 0, "github.com/acme/project/pull/42", ""),
        ]
        with patch.object(main, "git", side_effect=self.ready_git), \
             patch.object(main.operations, "tool", side_effect=responses):
            with self.assertRaisesRegex(RuntimeError, "HTTPS"):
                main.do_open_pr(state)

    def test_open_pr_links_backlog_issue_and_notes_pr(self):
        state = self.state() | {"item_id": "PVTI_1",
                                "item_url": "https://github.com/acme/project/issues/9"}
        responses = [
            subprocess.CompletedProcess([], 0, "[]", ""),
            subprocess.CompletedProcess([], 0, "https://github.com/acme/project/pull/42", ""),
        ]
        with patch.object(main, "git", side_effect=self.ready_git), \
             patch.object(main.operations, "tool", side_effect=responses) as process, \
             patch.object(main.task_source, "note_pr") as note_pr, \
             patch.object(main, "trail") as trail, \
             patch.object(main, "_enter_review_wait"):
            main.do_open_pr(state)

        trail.assert_called_once()
        self.assertEqual(trail.call_args.args[1], "PR opened: https://github.com/acme/project/pull/42")
        create = process.call_args_list[1].args[0]
        body = create[create.index("--body") + 1]
        self.assertIn("Closes https://github.com/acme/project/issues/9", body)
        note_pr.assert_called_once_with(state["item"], "PVTI_1",
                                         "https://github.com/acme/project/pull/42")

    def test_jira_pr_references_issue_without_relying_on_github_auto_close(self):
        state = self.state() | {"item_id": "10001", "item_key": "TEAM-7",
                                "item_url": "https://team.atlassian.net/browse/TEAM-7"}
        responses = [
            subprocess.CompletedProcess([], 0, "[]", ""),
            subprocess.CompletedProcess([], 0, "https://github.com/acme/project/pull/42", ""),
        ]
        with patch.object(main.config, "TASK_SOURCE", "jira"), \
             patch.object(main, "git", side_effect=self.ready_git), \
             patch.object(main.operations, "tool", side_effect=responses) as process, \
             patch.object(main.task_source, "note_pr") as note_pr, \
             patch.object(main, "_enter_review_wait"):
            main.do_open_pr(state)
        create = process.call_args_list[1].args[0]
        body = create[create.index("--body") + 1]
        self.assertIn("Closes TEAM-7", body)
        self.assertIn("Jira: https://team.atlassian.net/browse/TEAM-7", body)
        note_pr.assert_called_once_with(state["item"], "10001",
                                         "https://github.com/acme/project/pull/42")

    def test_open_pr_survives_backlog_write_back_failure(self):
        state = self.state() | {"pr_url": "https://github.com/acme/project/pull/42"}
        with patch.object(main, "git", side_effect=self.ready_git), \
             patch.object(main.task_source, "note_pr", side_effect=RuntimeError("gh down")), \
             patch.object(main, "_enter_review_wait") as enter_wait:
            main.do_open_pr(state)
        enter_wait.assert_called_once_with(state)
        self.assertNotIn("Closes", state["pr_summary"])

    def test_open_pr_restart_reuses_persisted_url_without_external_creation(self):
        state = self.state() | {"pr_url": "https://github.com/acme/project/pull/42"}
        with patch.object(main, "git", side_effect=self.ready_git) as git, \
             patch.object(main.operations, "tool") as process, \
             patch.object(main, "_enter_review_wait") as enter_wait:
            main.do_open_pr(state)

        self.assertFalse(any(c.args and c.args[0] == "push" for c in git.call_args_list))
        process.assert_not_called()
        enter_wait.assert_called_once_with(state)
        main._publish_existing_pr.assert_called_once_with(state)
        main._verify_pr_publication.assert_called_once_with(state)

    def test_open_pr_restart_discovers_existing_pr_before_create(self):
        state = self.state()
        existing = subprocess.CompletedProcess(
            [], 0, '[{"url":"https://github.com/acme/project/pull/42","state":"OPEN"}]', "")
        with patch.object(main, "git", side_effect=self.ready_git) as git, \
             patch.object(main.operations, "tool", return_value=existing) as process, \
             patch.object(main, "_enter_review_wait"):
            main.do_open_pr(state)

        self.assertIn(call("push", "-u", "origin", "codebot-api-version"), git.call_args_list)
        self.assertEqual(process.call_count, 1)
        self.assertEqual(state["pr_url"], "https://github.com/acme/project/pull/42")

    def test_closed_or_merged_pr_from_reused_branch_does_not_block_new_pr(self):
        for stale_state in ("CLOSED", "MERGED"):
            with self.subTest(stale_state=stale_state):
                state = self.state()
                responses = [
                    subprocess.CompletedProcess(
                        [], 0,
                        json.dumps([{
                            "url": "https://github.com/acme/project/pull/7",
                            "state": stale_state,
                        }]), ""),
                    subprocess.CompletedProcess(
                        [], 0, "https://github.com/acme/project/pull/42", ""),
                ]
                with patch.object(main, "git", side_effect=self.ready_git), \
                     patch.object(main.operations, "tool", side_effect=responses) as process, \
                     patch.object(main, "_enter_review_wait"):
                    main.do_open_pr(state)

                lookup = process.call_args_list[0].args[0]
                self.assertEqual(lookup[lookup.index("--state") + 1], "open")
                self.assertEqual(process.call_count, 2)
                self.assertEqual(state["pr_url"], "https://github.com/acme/project/pull/42")

    def test_open_pr_rejects_malformed_or_multiple_lookup_results_without_create(self):
        outputs = (
            "not-json",
            "{}",
            '[{"url":"https://github.com/acme/project/pull/42"}]',
            '[{"url":"https://github.com/acme/project/pull/42","state":"UNKNOWN"}]',
            '[{"url":"http://github.com/acme/project/pull/42","state":"OPEN"}]',
            ('[{"url":"https://github.com/acme/project/pull/42","state":"OPEN"},'
             '{"url":"https://github.com/acme/project/pull/43","state":"CLOSED"}]'),
        )
        for output in outputs:
            with self.subTest(output=output):
                state = self.state()
                response = subprocess.CompletedProcess([], 0, output, "")
                with patch.object(main, "git", side_effect=self.ready_git), \
                     patch.object(main.operations, "tool", return_value=response) as process:
                    with self.assertRaises(RuntimeError):
                        main.do_open_pr(state)
                self.assertEqual(process.call_count, 1)
                self.assertNotIn("pr_url", state)

    def test_open_pr_without_archive_path_returns_to_verifying_without_side_effects(self):
        state = self.state()
        state.pop("archive_path")
        state["review_gate_round"] = 2
        with patch.object(main, "git") as git, \
             patch.object(main.operations, "tool") as process:
            main.do_open_pr(state)

        git.assert_not_called()
        process.assert_not_called()
        self.assertEqual(state["state"], "VERIFYING")
        self.assertEqual(state.get("verify_round", 0), 0)
        self.assertNotIn("review_gate_round", state)

    def test_open_pr_with_missing_archive_directory_returns_to_archiving(self):
        state = self.state() | {"pr_url": "https://github.com/acme/project/pull/42"}
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(main.config, "REPO_PATH", Path(tmp)), \
             patch.object(main, "git") as git, patch.object(main.operations, "tool") as process, \
             patch.object(main, "_enter_review_wait"):
            main.do_open_pr(state)

        git.assert_not_called()
        process.assert_not_called()
        self.assertEqual(state["state"], "ARCHIVING")
        # The archive budget is per task; OPEN_PR no longer grants a fresh one.
        self.assertNotIn("archive_round", state)
        self.assertNotIn("pr_url", state)

    def test_open_pr_with_active_change_still_present_returns_to_archiving(self):
        state = self.state() | {"pr_url": "https://github.com/acme/project/pull/42"}
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / state["archive_path"]).mkdir(parents=True)
            (repo / "openspec/changes/api-version").mkdir(parents=True)
            with patch.object(main.config, "REPO_PATH", repo), \
                 patch.object(main, "git") as git, \
                 patch.object(main.operations, "tool") as process, \
                 patch.object(main, "_enter_review_wait"):
                main.do_open_pr(state)

        git.assert_not_called()
        process.assert_not_called()
        self.assertEqual(state["state"], "ARCHIVING")

    def test_open_pr_with_dirty_openspec_tree_returns_to_archiving_before_gh(self):
        state = self.state() | {"pr_url": "https://github.com/acme/project/pull/42"}
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / state["archive_path"]).mkdir(parents=True)
            with patch.object(main.config, "REPO_PATH", repo), \
                 patch.object(main, "git", return_value="?? openspec/specs/new/spec.md") as git, \
                 patch.object(main.operations, "tool") as process, \
                 patch.object(main, "_enter_review_wait"):
                main.do_open_pr(state)

        self.assertEqual(git.call_args_list, [
            call("status", "--porcelain", "--untracked-files=all", "--", "openspec/")])
        process.assert_not_called()
        self.assertEqual(state["state"], "ARCHIVING")

    def test_open_pr_with_archive_not_in_head_returns_to_archiving_before_gh(self):
        state = self.state() | {"pr_url": "https://github.com/acme/project/pull/42"}
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / state["archive_path"]).mkdir(parents=True)
            with patch.object(main.config, "REPO_PATH", repo), \
                 patch.object(main, "git", side_effect=["", ""]) as git, \
                 patch.object(main.operations, "tool") as process, \
                 patch.object(main, "_enter_review_wait"):
                main.do_open_pr(state)

        self.assertEqual(git.call_args_list, [
            call("status", "--porcelain", "--untracked-files=all", "--", "openspec/"),
            call("ls-tree", "-r", "--name-only", "HEAD", "--", state["archive_path"]),
        ])
        process.assert_not_called()
        self.assertEqual(state["state"], "ARCHIVING")

    def test_open_pr_with_committed_clean_archive_reuses_persisted_url(self):
        state = self.state() | {"pr_url": "https://github.com/acme/project/pull/42"}
        tracked = state["archive_path"] + "/proposal.md"
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / state["archive_path"]).mkdir(parents=True)
            with patch.object(main.config, "REPO_PATH", repo), \
                 patch.object(main, "git", side_effect=["", tracked]) as git, \
                 patch.object(main.operations, "tool") as process, \
                 patch.object(main, "_enter_review_wait") as enter_wait:
                main.do_open_pr(state)

        self.assertEqual(git.call_args_list, [
            call("status", "--porcelain", "--untracked-files=all", "--", "openspec/"),
            call("ls-tree", "-r", "--name-only", "HEAD", "--", state["archive_path"]),
        ])
        process.assert_not_called()
        enter_wait.assert_called_once_with(state)

    def test_open_pr_rejects_untrusted_archive_reference_before_push(self):
        state = self.state() | {"archive_path": "../../etc/passwd"}
        with patch.object(main.config, "REPO_PATH", Path("/safe/repo")), \
             patch.object(main, "git") as git, patch.object(main.operations, "tool") as process:
            with self.assertRaises(ValueError):
                main.do_open_pr(state)

        git.assert_not_called()
        process.assert_not_called()

    def test_post_pr_agent_fix_queues_coderbot_push(self):
        review = self.state() | {
            "state": "ADDRESS_REVIEW", "session_id": "session-1",
            "review_threads": [], "review_round": 0,
            "pr_url": "https://github.com/acme/project/pull/42",
        }
        agent_result = Mock(
            session_id="session-1", output="fixed", question=None, attachments=[])
        with patch.object(main.agent_runner, "resume", return_value=agent_result), \
             patch.object(main, "_scrub_evidence_from_repo", return_value=[]), \
             patch.object(main, "review_summary", return_value=""), \
             patch.object(main, "save_state") as save, \
             patch.object(main, "git", side_effect=["head-1", "head-2"]) as git, \
             patch.object(main, "_enter_review_wait"):
            main.do_address_review(review)

        self.assertNotIn(call("push", "origin", "codebot-api-version"), git.call_args_list)
        self.assertEqual(review["state"], "PUSHING")
        self.assertEqual(review["push_context"]["continuation"], "review")
        save.assert_called_once_with(review)

    def test_human_feedback_fix_queues_coderbot_push_with_serializable_result(self):
        state = self.state() | {
            "state": "WAIT_MERGE", "session_id": "session-1",
            "pr_url": "https://github.com/acme/project/pull/42",
        }
        verdict = Mock(output='{"action":"changes","feedback":"adjust it"}')
        fixed = Mock(session_id="session-1", output="fixed", question=None, attachments=[])
        with patch.object(main.agent_runner, "run", return_value=verdict), \
             patch.object(main.agent_runner, "resume", return_value=fixed), \
             patch.object(main, "_scrub_evidence_from_repo", return_value=[]), \
             patch.object(main, "_collect_attachments",
                          return_value=[Path("/tmp/evidence.txt")]), \
             patch.object(main, "save_state"), patch.object(main, "email"), \
             patch.object(main, "git") as git:
            main.do_merge_reply(state, "please adjust it")

        git.assert_not_called()
        self.assertEqual(state["state"], "PUSHING")
        self.assertEqual(state["push_context"]["continuation"], "feedback")
        self.assertEqual(state["push_context"]["attachments"], ["/tmp/evidence.txt"])
        json.dumps(state["push_context"])

    def test_human_feedback_question_resumes_to_push_and_wait_for_merge(self):
        state = self.state() | {
            "state": "WAIT_MERGE", "session_id": "session-1",
            "pr_url": "https://github.com/acme/project/pull/42",
        }
        verdict = Mock(output='{"action":"changes","feedback":"adjust it"}')
        question = Mock(
            session_id="session-1", output="question", question="Which value?", attachments=[])
        fixed = Mock(session_id="session-1", output="fixed", question=None, attachments=[])
        with patch.object(main.agent_runner, "run", return_value=verdict), \
             patch.object(main.agent_runner, "resume", side_effect=[question, fixed]), \
             patch.object(main, "_scrub_evidence_from_repo", return_value=[]), \
             patch.object(main, "_collect_attachments", return_value=[]), \
             patch.object(main, "save_state"), patch.object(main, "email"), \
             patch.object(main, "git") as git:
            main.do_merge_reply(state, "please adjust it")
            self.assertEqual(state["return_state"], "APPLY_PR_FEEDBACK")
            main._handle_reply(state, "Use 42")

        git.assert_not_called()
        self.assertEqual(state["state"], "PUSHING")
        self.assertEqual(state["push_context"]["continuation"], "feedback")

    def test_unresolved_thread_fix_queues_coderbot_push(self):
        state = self.state() | {
            "state": "ADDRESS_PR_THREADS", "session_id": "session-1",
            "pr_threads": [], "pr_url": "https://github.com/acme/project/pull/42",
        }
        fixed = Mock(session_id="session-1", output="fixed", question=None, attachments=[])
        with patch.object(main.agent_runner, "resume", return_value=fixed), \
             patch.object(main, "_scrub_evidence_from_repo", return_value=[]), \
             patch.object(main, "_finish_address_pr_threads"), \
             patch.object(main, "save_state"), \
             patch.object(main, "git") as git:
            main.do_address_pr_threads(state)

        git.assert_not_called()
        self.assertEqual(state["state"], "PUSHING")
        self.assertEqual(state["push_context"]["continuation"], "threads")


class PushPhaseTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(main.final_checks, "run", return_value={"status": "pass"})
        patcher.start()
        self.addCleanup(patcher.stop)
    def state(self, continuation, **context):
        return {
            "state": "PUSHING",
            "branch": "codebot-api-version",
            "pr_url": "https://github.com/acme/project/pull/42",
            "push_context": {"continuation": continuation, **context},
        }

    def test_review_push_continues_to_review_wait_without_agent_resume(self):
        state = self.state("review") | {"review_threads": [{"id": "T1", "body": "fix"}]}
        with patch.object(main, "git") as git, \
             patch.object(main.agent_runner, "resume") as resume, \
             patch.object(main, "_resolve_review_threads") as resolve, \
             patch.object(main, "_enter_review_wait") as enter:
            main.do_push(state)

        git.assert_any_call("push", "origin", "codebot-api-version")
        resume.assert_not_called()
        resolve.assert_called_once()
        enter.assert_called_once_with(state)
        self.assertNotIn("review_threads", state)
        self.assertNotIn("push_context", state)

    def test_feedback_push_restores_paths_emails_and_waits_for_merge(self):
        state = self.state(
            "feedback", output="fixed details", attachments=["/tmp/evidence.txt"])
        with patch.object(main, "git"), patch.object(main, "email") as email:
            main.do_push(state)

        args = email.call_args.args
        self.assertIn("fixed details", args[2])
        self.assertEqual(args[3][0], Path("/tmp/evidence.txt"))
        self.assertEqual(args[3][-1].suffix, ".md")
        self.assertEqual(state["state"], "WAIT_MERGE")
        self.assertNotIn("push_context", state)

    def test_thread_push_finishes_threads_without_agent_resume(self):
        state = self.state("threads")
        with patch.object(main, "git"), \
             patch.object(main.agent_runner, "resume") as resume, \
             patch.object(main, "_finish_address_pr_threads") as finish:
            main.do_push(state)

        resume.assert_not_called()
        finish.assert_called_once_with(state)
        self.assertNotIn("push_context", state)

    def test_push_failure_keeps_persisted_context_for_retry(self):
        state = self.state("review")
        context = dict(state["push_context"])
        with patch.object(main, "git", side_effect=RuntimeError("network down")), \
             patch.object(main.agent_runner, "resume") as resume:
            with self.assertRaisesRegex(RuntimeError, "network down"):
                main.do_push(state)

        resume.assert_not_called()
        self.assertEqual(state["state"], "PUSHING")
        self.assertEqual(state["push_context"], context)

    def test_restart_in_pushing_retries_only_push_then_continuation(self):
        state = self.state("feedback", output="fixed", attachments=[])
        with patch.object(main, "git") as git, \
             patch.object(main.agent_runner, "resume") as resume, patch.object(main, "email"):
            main.PHASES[state["state"]](state)

        git.assert_any_call("push", "origin", "codebot-api-version")
        resume.assert_not_called()
        self.assertEqual(state["state"], "WAIT_MERGE")

    def test_wait_reply_review_completion_queues_push(self):
        state = self.state("unused") | {
            "state": "WAIT_REPLY", "return_state": "ADDRESS_REVIEW",
            "session_id": "session-1", "review_comments": [],
        }
        state.pop("push_context")
        fixed = Mock(session_id="session-1", output="fixed", question=None, attachments=[])
        with patch.object(main.agent_runner, "run", return_value=Mock(output='{"action":"answer"}')), \
             patch.object(main.agent_runner, "resume", return_value=fixed), \
             patch.object(main, "_scrub_evidence_from_repo", return_value=[]), \
             patch.object(main, "email"), \
             patch.object(main, "save_state"), patch.object(main, "git") as git:
            main._handle_reply(state, "use the safe option")

        git.assert_not_called()
        self.assertEqual(state["state"], "PUSHING")
        self.assertEqual(state["push_context"]["continuation"], "review")
        self.assertNotIn("return_state", state)

    def test_wait_reply_thread_completion_queues_push(self):
        state = self.state("unused") | {
            "state": "WAIT_REPLY", "return_state": "ADDRESS_PR_THREADS",
            "session_id": "session-1", "pr_threads": [],
        }
        state.pop("push_context")
        fixed = Mock(session_id="session-1", output="fixed", question=None, attachments=[])
        with patch.object(main.agent_runner, "run", return_value=Mock(output='{"action":"answer"}')), \
             patch.object(main.agent_runner, "resume", return_value=fixed), \
             patch.object(main, "_scrub_evidence_from_repo", return_value=[]), \
             patch.object(main, "email"), \
             patch.object(main, "save_state"), patch.object(main, "git") as git:
            main._handle_reply(state, "apply it")

        git.assert_not_called()
        self.assertEqual(state["state"], "PUSHING")
        self.assertEqual(state["push_context"]["continuation"], "threads")
        self.assertNotIn("return_state", state)

    def test_success_cleanup_removes_stale_push_context(self):
        state = self.state("review") | {
            "state": "WAIT_MERGE", "item": "Support another API version",
            "slug": "api-version", "session_id": "session-1",
        }
        verdict = Mock(output='{"action":"merge","feedback":""}')
        merged = subprocess.CompletedProcess([], 0, '{"state":"MERGED","mergeable":"MERGEABLE"}', "")
        with patch.object(main.agent_runner, "run", return_value=verdict), \
             patch.object(main.operations, "tool", return_value=merged), \
             patch.object(main.task_source, "mark_done", return_value=True), \
             patch.object(main, "email"):
            main.do_merge_reply(state, "merge")

        self.assertEqual(state["state"], "IDLE")
        self.assertNotIn("push_context", state)


if __name__ == "__main__":
    unittest.main()


LONG_ITEM = ("CodeBot[2] Implementar un «sitio Sistema» que vive dentro del mismo SPA bajo "
             "la ruta #/sistema, con un shell propio que reproduce el sidebar del sitio "
             "principal; " + "y muchas cosas más " * 40)


class PullRequestTitleTests(unittest.TestCase):
    def test_clamp_never_exceeds_github_limit_and_strips_tags(self):
        title = main._clamp_pr_title("CodeBot[2] " + "palabra " * 100)
        self.assertLessEqual(len(title), main.PR_TITLE_HARD_MAX)
        self.assertTrue(title.endswith("…"))
        self.assertNotIn("CodeBot", title)
        self.assertEqual(main._clamp_pr_title("Codebot[3]: Fix   the\nthing"), "Fix the thing")

    def test_fallback_uses_first_clause_within_target(self):
        title = main._fallback_pr_title(LONG_ITEM)
        self.assertLessEqual(len(title), main.PR_TITLE_TARGET)
        self.assertTrue(title.startswith("Implementar un «sitio Sistema»"))

    def test_short_item_is_used_verbatim_without_agent(self):
        state = {"item": "Support another API version"}
        with patch.object(main.agent_runner, "run") as run:
            self.assertEqual(main._pr_title(state), "Support another API version")
        run.assert_not_called()
        self.assertEqual(state["pr_title"], "Support another API version")

    def test_long_item_asks_agent_for_summary_and_caches_it(self):
        state = {"item": LONG_ITEM}
        reply = Mock(output='{"title": "Añadir rol sysadmin y sitio Sistema"}')
        with patch.object(main.agent_runner, "run", return_value=reply) as run:
            self.assertEqual(main._pr_title(state), "Añadir rol sysadmin y sitio Sistema")
        self.assertIn("sitio Sistema", run.call_args.args[0])
        self.assertEqual(state["pr_title"], "Añadir rol sysadmin y sitio Sistema")
        with patch.object(main.agent_runner, "run") as run:
            main._pr_title(state)
        run.assert_not_called()

    def test_agent_failure_or_overlong_answer_still_yields_a_valid_title(self):
        state = {"item": LONG_ITEM}
        with patch.object(main.agent_runner, "run", side_effect=RuntimeError("boom")):
            title = main._pr_title(state)
        self.assertTrue(0 < len(title) <= main.PR_TITLE_TARGET)
        state = {"item": LONG_ITEM}
        reply = Mock(output=json.dumps({"title": "x" * 1000}))
        with patch.object(main.agent_runner, "run", return_value=reply):
            title = main._pr_title(state)
        self.assertLessEqual(len(title), main.PR_TITLE_HARD_MAX)

    def test_guidance_forces_regeneration_with_the_users_words(self):
        state = {"item": "Support another API version",
                 "pr_title_guidance": "use a simple summary of what we are doing"}
        reply = Mock(output='{"title": "Add API v2 support"}')
        with patch.object(main.agent_runner, "run", return_value=reply) as run:
            self.assertEqual(main._pr_title(state), "Add API v2 support")
        self.assertIn("simple summary", run.call_args.args[0])


class LongTitlePullRequestTests(NativePullRequestTests):
    """Every native-PR scenario again, with a backlog bullet far beyond GitHub's limit."""

    def setUp(self):
        super().setUp()
        # The one-shot title call must never eat the mocked `gh` subprocess responses.
        self.agent_patch = patch.object(
            main.agent_runner, "run",
            return_value=Mock(output='{"title": "Añadir rol sysadmin y sitio Sistema"}'))
        self.agent_patch.start()

    def tearDown(self):
        self.agent_patch.stop()
        super().tearDown()

    def state(self):
        return super().state() | {"item": LONG_ITEM}

    def test_native_pr_pushes_branch_and_stores_https_url(self):
        state = self.state()
        responses = [
            subprocess.CompletedProcess([], 0, "[]\n", ""),
            subprocess.CompletedProcess([], 0, "https://github.com/acme/project/pull/42\n", ""),
        ]
        with patch.object(main, "git", side_effect=self.ready_git), \
             patch.object(main.operations, "tool", side_effect=responses) as process, \
             patch.object(main, "_enter_review_wait"):
            main.do_open_pr(state)
        command = process.call_args_list[1].args[0]
        title = command[command.index("--title") + 1]
        self.assertEqual(title, "Añadir rol sysadmin y sitio Sistema")
        self.assertIn(LONG_ITEM, command[command.index("--body") + 1])

    def test_title_is_never_over_the_limit_even_without_agent(self):
        state = self.state()
        responses = [
            subprocess.CompletedProcess([], 0, "[]\n", ""),
            subprocess.CompletedProcess([], 0, "https://github.com/acme/project/pull/42\n", ""),
        ]
        with patch.object(main, "git", side_effect=self.ready_git), \
             patch.object(main.operations, "tool", side_effect=responses) as process, \
             patch.object(main.agent_runner, "run", side_effect=RuntimeError("down")), \
             patch.object(main, "_enter_review_wait"):
            main.do_open_pr(state)
        command = process.call_args_list[1].args[0]
        self.assertLessEqual(len(command[command.index("--title") + 1]), main.PR_TITLE_HARD_MAX)
