"""Controlled workspace lifecycle: synthetic Git repos and fake external adapters."""
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import errors
import lifecycle
import main
import workspace_runner as runner
from execution_identity import snapshot
from workspace import WorkspaceError, discover
from workspace_state import bind_and_validate
from tests.test_workspace import git, repository


class FakeGitHub:
    """PRs per repository, pinned by number; URLs come back in GitHub's canonical case."""
    def __init__(self):
        self.prs, self.calls, self.pushed = {}, [], {}

    def push(self, ops, branch):
        self.pushed[(ops.name, branch)] = git(ops.path, "rev-parse", "HEAD")
        for pr in self.prs.values():
            if pr["repo"] == ops.name and pr["head"] == branch and pr["state"] == "OPEN":
                pr["sha"] = self.pushed[(ops.name, branch)]

    def gh(self, ops, *args):
        self.calls.append((ops.name, *args))
        option = lambda name: args[args.index(name) + 1]
        if args[:2] == ("pr", "list"):
            return json.dumps([{"number": n} for (repo, n), pr in self.prs.items()
                               if repo == ops.name and pr["head"] == option("--head") and pr["state"] == "OPEN"])
        if args[:2] == ("pr", "create"):
            number = len(self.prs) + 1
            self.prs[(ops.name, number)] = {"repo": ops.name, "head": option("--head"), "state": "OPEN",
                "draft": "--draft" in args, "sha": self.pushed.get((ops.name, option("--head"))),
                "mergeable": "MERGEABLE", "body": option("--body")}
            return f"https://github.com/example/{ops.name}/pull/{number}"
        pr = self.prs[(ops.name, int(args[2]))]
        if args[:2] == ("pr", "view"):
            return json.dumps({"number": int(args[2]), "url": f"https://github.com/Example/{ops.name.upper()}/pull/{args[2]}",
                               "state": pr["state"], "isDraft": pr["draft"], "mergeable": pr["mergeable"],
                               "headRefName": pr["head"], "baseRefName": "main", "headRefOid": pr["sha"],
                               "isCrossRepository": False, "statusCheckRollup": pr.get("checks", [])})
        if args[:2] == ("pr", "ready"):
            pr["draft"] = False
        elif args[:2] == ("pr", "edit"):
            pr["body"] = option("--body")
        elif args[:2] == ("pr", "merge"):
            if pr.get("fail_merge"):
                raise errors.AgentFault("merge refused: base branch policy")
            pr["state"] = "MERGED"
        return ""


class WorkspaceRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / "workspace"
        self.root.mkdir()
        self.data = self.root.parent / "data"
        self.data.mkdir()
        self.api = repository(self.root / "api", remote="https://github.com/example/api.git")
        self.ui = repository(self.root / "ui", remote="https://github.com/example/ui.git")
        self.unused = repository(self.root / "unused", remote="https://github.com/example/unused.git")
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name, value in {"REPO_PATH": self.root, "DATA_DIR": self.data, "STATE_PATH": self.data / "state.json",
                            "HOLDS_PATH": self.data / "holds.json", "TARGET_MODE": "workspace",
                            "WORKSPACE_MANIFEST": None}.items():
            self.stack.enter_context(patch.object(main.config, name, value))
        self.stack.enter_context(patch.object(main, "email", Mock()))
        self.stack.enter_context(patch.object(main, "trail", Mock()))
        self.stack.enter_context(patch.object(main, "announce_milestone", Mock()))
        self.stack.enter_context(patch.object(main, "task_source", Mock()))
        self.stack.enter_context(patch.object(main, "_ensure_task_language", Mock()))
        # The fake origins are unreachable; OpenSpec validation has its own walk.
        self.stack.enter_context(patch.object(lifecycle.RepoOps, "fetch", return_value=False))
        self.stack.enter_context(patch.object(runner, "validate_plan", return_value=None))
        self.github = FakeGitHub()
        self.stack.enter_context(patch.object(lifecycle.RepoOps, "gh", lambda ops, *args: self.github.gh(ops, *args)))
        self.stack.enter_context(patch.object(lifecycle.RepoOps, "push", lambda ops, branch: self.github.push(ops, branch)))
        self.context = discover(self.root)
        self.state = bind_and_validate({"state": "IDLE"}, self.context)
        self.state.update(item="Feature", slug="feature", branch="bot-feature", state="IMPLEMENTING",
                          execution_task_id="task", session_id="session")
        self.state["workspace"]["baseline"] = runner.inspect_repositories(self.context)

    def edit(self, path):
        git(path, "checkout", "-b", "bot-feature")
        (path / "source.txt").write_text("feature")
        git(path, "add", "source.txt")
        git(path, "commit", "-m", "feature")

    def records(self):
        runner.record_changes(self.state, self.context, self.state["workspace"]["baseline"])

    def verified(self):
        self.state["workspace"]["verified_snapshot"] = snapshot(self.root)

    def test_full_workspace_writes_and_automatic_subset_tracking(self):
        before = runner.inspect_repositories(self.context)
        self.edit(self.api)
        self.edit(self.ui)
        runner.record_changes(self.state, self.context, before)
        self.assertEqual(set(self.state["workspace"]["repositories"]), {"api", "ui"})
        self.assertEqual(git(self.unused, "branch", "--show-current"), "main")
        self.assertIn("All files and child repositories", runner.instructions(self.state, self.context))
        self.assertFalse((self.root / ".git").exists())

    def test_malformed_plan_does_not_hide_question_or_erase_previous_checks(self):
        self.state["reported_check_plan"] = [{"id": "existing", "argv": ["test"]}]
        result = SimpleNamespace(session_id="session", output="CHECK_PLAN: \nNEED_USER_INPUT: Which behavior?",
                                 question="Which behavior?", attachments=[], preamble="")
        with patch.object(main, "_maybe_request_credential", return_value=False):
            self.assertTrue(main.handle_result(self.state, result, "EXPLORING"))
        self.assertEqual(self.state["state"], "WAIT_REPLY")
        self.assertEqual(self.state["reported_check_plan"][0]["id"], "existing")
        self.assertIn("reported_check_plan_error", self.state)

    def test_dirty_repositories_wait_in_wait_clean_instead_of_failing_every_tick(self):
        (self.ui / "scratch.txt").write_text("unsaved")
        state = bind_and_validate({"state": "IDLE"}, self.context)
        with patch.object(main, "_load_holds", return_value=[]):
            runner.pick(state)
            self.assertEqual(state["state"], "WAIT_CLEAN")
            runner.pick(state)  # a second dirty pick does not announce again
            runner.run_phase(state)  # still dirty: no exception, still waiting
        self.assertEqual(state["state"], "WAIT_CLEAN")
        self.assertEqual(main.email.call_count, 1)
        (self.ui / "scratch.txt").unlink()
        runner.run_phase(state)
        self.assertEqual(state["state"], "IDLE")

    def test_pick_after_the_dirty_notice_starts_the_task(self):
        # The dirty notice opened a thread on the idle state; the next pick must still bind
        # a fresh workspace (it raised TargetMismatch and saved a state no tick could load).
        state = bind_and_validate({"state": "IDLE"}, self.context)
        state.update(thread_id="thread-1", pending_decision={"wait_state": "WAIT_CLEAN"})
        main.task_source.list_pending_items.return_value = [{"text": "Feature", "id": "TASK-1"}]
        main.task_source.claim_task.return_value = True
        with patch.object(main, "_load_holds", return_value=[]):
            runner.pick(state)
        self.assertEqual(state["state"], "EXPLORING")
        self.assertEqual(bind_and_validate(state, self.context)["workspace"]["repositories"], {})

    def ok(self):
        return SimpleNamespace(session_id="session", output="Repaired", question=None, attachments=[], data=None)

    def test_controller_problems_go_to_one_bounded_repair_turn_not_agent_verification(self):
        self.state.update(state="VERIFYING", reported_check_plan_error="zip-browser: unsupported timeout_millis",
                          pending_feedback="Keep ZIP scope.")
        prompts = []
        def repaired(state, prompt, fresh=False):
            prompts.append(prompt)
            return self.ok()
        with patch.object(main.config, "QUALITY_GATE_MAX_ROUNDS", 2), patch.object(runner, "turn", side_effect=repaired), \
             patch("checks.execute_plan") as execute:
            for _ in range(3):
                runner.verify(self.state)
                if self.state["state"] == "REPAIR_CHECKS":
                    runner.repair_checks(self.state)
        execute.assert_not_called()
        self.assertEqual(len(prompts), 2)
        self.assertIn("timeout_millis", prompts[0])
        self.assertIn("No root integration", prompts[0])
        self.assertIn("Keep ZIP scope.", prompts[0])  # owner guidance is used once, then cleared
        self.assertNotIn("pending_feedback", self.state)
        self.assertIn("repair attempt 2", prompts[1])
        self.assertEqual(self.state["state"], "WAIT_STUCK")
        main._clear_stuck(self.state, "VERIFYING")
        self.assertNotIn("verification_rounds", self.state["workspace"])

    def test_failed_and_blocked_checks_are_repaired_with_the_controller_report(self):
        from workspace_checks import HarnessBlocked
        self.edit(self.api)
        self.records()
        self.state.update(state="VERIFYING", reported_check_plan=[{"id": "integration", "argv": ["true"], "cwd": "."}])
        with patch.object(runner, "validate_check", side_effect=HarnessBlocked("shared volume")), \
             patch("checks.execute_plan") as execute:
            runner.verify(self.state)
        execute.assert_not_called()
        self.assertEqual(self.state["state"], "REPAIR_CHECKS")
        self.assertIn("shared volume", json.dumps(self.state["check_repair"]["checks"]))
        self.state["state"] = "VERIFYING"
        failure = {"check": "integration", "status": "fail", "exit_code": 1, "report": "failure.log"}
        def run(*args, **kwargs):
            (self.root / "shared-source.txt").write_text("changed during checks")
            return [failure]
        with patch("checks.execute_plan", side_effect=run):
            runner.verify(self.state)
        self.assertEqual(self.state["state"], "REPAIR_CHECKS")
        self.assertIn("failure.log", json.dumps(self.state["check_repair"]["checks"]))
        self.assertTrue(self.state["final_check_report"]["source_changed"])
        self.assertNotIn("verified_snapshot", self.state["workspace"])
        with patch.object(runner, "turn", return_value=self.ok()) as turn:
            runner.repair_checks(self.state)
        self.assertIn("failure.log", turn.call_args.args[1])
        self.assertEqual(self.state["state"], "VERIFYING")

    def test_retry_resets_plan_counter_not_errors_evidence_or_full_audit_budget(self):
        self.state.update(state="WAIT_STUCK", stuck_return="VERIFYING", check_plan_repair_round=3,
            check_plan_repair_revision=7, reported_check_plan_error="zip-browser: unsupported timeout_millis",
            reported_check_plan=[{"id": "existing", "argv": ["test"]}],
            final_check_report={"status": "indeterminate", "checks": [{"check": "unit", "status": "pass"}]})
        store = main.phase_checkpoint.store()
        for _ in range(2):
            store.put("operation", task_id="task", identity="full-e2e-launch", data={})
        main._clear_stuck(self.state, "VERIFYING")
        self.assertEqual(self.state.get("check_plan_repair_round", 0), 0)
        self.assertEqual(self.state["check_plan_repair_revision"], 8)
        self.assertIn("timeout_millis", self.state["reported_check_plan_error"])
        self.assertEqual(self.state["reported_check_plan"][0]["id"], "existing")
        self.assertEqual(self.state["final_check_report"]["checks"][0]["status"], "pass")
        self.assertEqual(len(store.list("operation", "task", identity="full-e2e-launch")), 2)

    def test_unselected_full_check_is_not_validated_as_required_feature_execution(self):
        self.state.update(state="VERIFYING", reported_check_plan=[
            main.check_plan.Check("full", ["playwright", "test"], kind="e2e").to_dict(),
            main.check_plan.Check("focused", ["playwright", "test", "feature.spec.ts"], kind="e2e",
                                  scope="focused").to_dict()])
        self.state["workspace"]["reviewed_snapshot"] = snapshot(self.root)
        def validate(context, check):
            self.assertNotEqual(check.id, "full")
        with patch.object(runner, "validate_check", side_effect=validate), \
             patch("checks.execute_plan", return_value=[{"check": "focused", "status": "pass", "test_count": 2}]):
            runner.verify(self.state)
        self.assertEqual(self.state["state"], "ARCHIVING")

    def test_hold_preserves_repair_revision_and_clears_it_from_idle_state(self):
        self.state["check_plan_repair_revision"] = 7
        with patch.object(main, "_release_credentials"), patch.object(main, "_publish_progress"):
            main._hold_task(self.state, "C123:1")
        saved = json.loads(main.config.HOLDS_PATH.read_text())[0]["saved"]
        self.assertEqual(saved["check_plan_repair_revision"], 7)
        self.assertNotIn("check_plan_repair_revision", self.state)

    def test_question_reply_routes_without_lateral_worker(self):
        self.state.update(state="WAIT_REPLY", return_state="EXPLORING", pending_question="What scope?", thread_id="C123:1")
        with patch.object(main.gmail_client, "mark_processed"), patch.object(main, "_conversation_worker", None):
            self.assertTrue(main._queue_conversation(self.state, "reply", "Only frontend, not backend"))
        row = main.phase_checkpoint.store().list("conversation", "task")[0]
        self.assertEqual(row["data"]["route"], "flow")
        with patch.object(main, "_dispatch_wait_reply") as dispatch:
            self.assertTrue(main._dispatch_conversation(self.state))
        dispatch.assert_called_once_with(self.state, "reply", "Only frontend, not backend")

    def test_restart_recovers_pending_question_reply_but_not_btw(self):
        import parallel_conversation
        self.state.update(state="WAIT_REPLY", return_state="EXPLORING", pending_question="What scope?", thread_id="C123:1")
        store = main.phase_checkpoint.store()
        pending = parallel_conversation.receive(store, self.state, self.root, "reply", "Only frontend")
        aside = parallel_conversation.receive(store, self.state, self.root, "aside", "/btw explain")
        with patch.object(main, "_dispatch_wait_reply") as dispatch:
            self.assertTrue(main._dispatch_conversation(self.state))
        dispatch.assert_called_once_with(self.state, "reply", "Only frontend")
        self.assertEqual(store.get("conversation", pending["id"])["status"], "complete")
        self.assertIsNone(store.get("conversation", aside["id"])["data"]["route"])

    def test_pick_only_accepts_actual_backlog_item_and_preserves_branches(self):
        self.state = {"state": "IDLE"}
        main.task_source.list_pending_items.return_value = [
            {"text": "Feature", "id": "TASK-1", "detail": "Change API and UI"}]
        main.task_source.claim_task.return_value = True
        with patch.object(main.gmail_client, "open_thread", return_value="C123:1"):
            runner.pick(self.state)
        self.assertEqual(self.state["item_id"], "TASK-1")
        self.assertEqual(self.state["state"], "EXPLORING")
        self.assertEqual(git(self.api, "branch", "--show-current"), "main")
        self.assertEqual(self.state["workspace"]["repositories"], {})

    def test_hold_saves_workspace_without_reset_and_resume_uses_saved_context(self):
        with patch.object(main, "_release_credentials"), patch.object(main, "_publish_progress"), \
             patch.object(main, "_reset_to_base_branch") as reset, patch.object(main, "_commit_pending_work") as commit:
            main._hold_task(self.state, "C123:1")
        reset.assert_not_called()
        commit.assert_not_called()
        hold = json.loads(main.config.HOLDS_PATH.read_text())[0]
        self.assertIn("workspace", hold["saved"])
        self.assertEqual(self.state["state"], "IDLE")

    def test_turn_records_late_edits_and_shared_local_journal(self):
        (self.root / "scripts").mkdir()
        file = self.root / "scripts/run.sh"
        file.write_text("echo before\n")
        def invoke(prompt):
            self.assertIn("no repository-selection permission gate", prompt)
            self.edit(self.api)
            file.write_text("echo after\n")
            return SimpleNamespace(session_id="new-session", output="done")
        with patch.object(main.agent_runner, "resume", side_effect=lambda sid, prompt: invoke(prompt)):
            runner.turn(self.state, "Implement")
        self.assertIn("api", self.state["workspace"]["repositories"])
        self.assertEqual(self.state["session_id"], "new-session")
        edit = self.state["workspace"]["local_edits"][0]
        self.assertEqual(Path(edit["journal"]).stat().st_mode & 0o777, 0o600)
        file.write_text("user changed it\n")
        with self.assertRaisesRegex(WorkspaceError, "overwrite"):
            runner.restore_local(self.context, edit["journal"])
        file.write_text("echo after\n")
        runner.restore_local(self.context, edit["journal"])
        self.assertEqual(file.read_text(), "echo before\n")

    def test_secrets_not_journaled(self):
        (self.root / "scripts").mkdir()
        (self.root / "scripts/key.pem").write_text("private")
        (self.root / ".env").write_text("SECRET=secret")
        self.assertNotIn(".env", runner._shared_files(self.context))
        self.assertNotIn("scripts/key.pem", runner._shared_files(self.context))

    def test_snapshot_includes_dependency_and_local_changes_not_env_or_outputs(self):
        before = snapshot(self.root)
        (self.unused / "source.txt").write_text("dependency changed")
        self.assertNotEqual(snapshot(self.root), before)
        before = snapshot(self.root)
        (self.root / "docker-compose.yaml").write_text("services: {}")
        self.assertNotEqual(snapshot(self.root), before)
        before = snapshot(self.root)
        (self.root / ".env").write_text("SECRET=private")
        self.assertEqual(snapshot(self.root), before)

    @patch("checks.tool_versions", return_value={"executable": "sh", "version": "test"})
    def test_checks_run_again_when_another_repository_changes(self, tool_versions):
        import checks
        import check_plan
        store = main.phase_checkpoint.store()
        check = check_plan.Check("integration", ["sh", "-c", "echo OK"], cwd=".")
        first = checks.execute(check, self.root, store, "task")
        second = checks.execute(check, self.root, store, "task")
        self.assertFalse(first["reused"])
        self.assertTrue(second["reused"])
        (self.ui / "source.txt").write_text("changed")
        third = checks.execute(check, self.root, store, "task")
        self.assertFalse(third["reused"])
        self.assertEqual(third["repetition_reason"], "content_changed")
        self.assertEqual(tool_versions.call_count, 3)

    def test_unsafe_harness_rejected_without_execution(self):
        import check_plan
        e2e = self.root / "e2e"
        e2e.mkdir()
        (e2e / "run.sh").write_text("docker compose down --volumes\n")
        with self.assertRaisesRegex(WorkspaceError, "project"):
            runner.validate_check(self.context, check_plan.Check("e2e", ["./run.sh"], cwd="e2e"))

    def test_dedicated_cleanup_and_guarded_open_are_allowed(self):
        import check_plan
        e2e = self.root / "e2e"
        e2e.mkdir()
        (self.root / "docker-compose.e2e.yaml").write_text("services: {}\n")
        (e2e / "run.sh").write_text(
            'COMPOSE="docker compose -p feature-e2e -f $ROOT/docker-compose.e2e.yaml"\n'
            '$COMPOSE down --volumes\n'
            'if [ "$(uname -s)" = "Darwin" ] && command -v open >/dev/null; then\n'
            ' open report.html\nfi\n')
        value = {"name": "feature-e2e", "services": {}, "volumes": {"data": {"name": "feature-e2e_data"}}}
        def command(root, argv):
            if "compose" in argv:
                return json.dumps(value)
            if "inspect" in argv:
                return '"unix:///var/run/docker.sock"'
            return "default"
        with patch.object(runner, "command", side_effect=command), patch.dict("os.environ", {}, clear=True):
            runner.validate_check(self.context, check_plan.Check("e2e", ["./run.sh"], cwd="e2e"))

    def test_abort_and_finish_preserve_files_and_do_not_run_root_git(self):
        with patch.object(main, "_reset_to_base_branch") as reset, \
             patch.object(main, "_release_credentials"), patch.object(main, "_publish_progress"):
            main._abort_and_reset(self.state, "Stop")
        reset.assert_not_called()
        self.assertEqual(self.state["state"], "IDLE")
        self.assertNotIn("workspace", self.state)
        self.assertEqual(git(self.api, "branch", "--show-current"), "main")

    def test_verification_requires_shared_check_and_binds_evidence(self):
        self.edit(self.api)
        self.records()
        self.state["reported_check_plan"] = [{"id": "integration", "argv": ["sh", "-c", "echo OK"], "cwd": "."}]
        self.state["state"] = "VERIFYING"
        self.state["workspace"]["reviewed_snapshot"] = snapshot(self.root)
        runner.verify(self.state)
        self.assertEqual(self.state["state"], "ARCHIVING")
        self.assertEqual(self.state["workspace"]["verified_snapshot"], snapshot(self.root))

    def test_confirmed_local_baseline_debt_is_nonblocking_but_composite_unknown_is_not(self):
        from check_plan import Check
        failed = {"status": "fail", "failures": {"old": "baseline"}}
        with patch.object(main.final_checks, "baseline_result", return_value=failed):
            local = runner.workspace_gate(Check("api-unit", ["test"], cwd="api", inputs=["api"]), failed,
                                          self.state, self.context, main.phase_checkpoint.store())
            shared = runner.workspace_gate(Check("integration", ["test"]), failed, self.state,
                                           self.context, main.phase_checkpoint.store())
        self.assertEqual(local["status"], "pass")
        self.assertEqual(shared["status"], "indeterminate")
        self.assertEqual(shared["reason"], "immutable_composite_baseline_missing")

    def test_planning_archive_does_not_invalidate_implementation_publication_identity(self):
        from execution_identity import verification_snapshot
        before = verification_snapshot(self.root)
        path = self.root / "openspec/changes/feature/tasks.md"
        path.parent.mkdir(parents=True)
        path.write_text("- [x] Done\n")
        self.assertEqual(verification_snapshot(self.root), before)
        explicit = verification_snapshot(self.root, ["openspec"])
        path.write_text("- [x] Done\n- [ ] Newly approved behavior\n")
        self.assertNotEqual(verification_snapshot(self.root, ["openspec"]), explicit)

    def test_controller_creates_a_unique_branch_and_restores_base(self):
        before = runner.inspect_repositories(self.context)
        base = git(self.api, "rev-parse", "HEAD")
        (self.api / "source.txt").write_text("agent edit on main")
        git(self.api, "commit", "-am", "agent committed on main")
        runner.record_changes(self.state, self.context, before)
        record = self.state["workspace"]["repositories"]["api"]
        self.assertEqual(git(self.api, "branch", "--show-current"), "bot-feature")
        self.assertEqual(git(self.api, "rev-parse", "main"), base)  # local base untouched
        self.assertEqual(record["base_sha"], base)
        self.assertNotIn("ui", self.state["workspace"]["repositories"])
        # A finished task's children go back to base before the next pick; a new
        # pick of the same item gets a different branch.
        main.task_source.list_pending_items.return_value = [{"text": "Feature", "id": "TASK-1"}]
        main.task_source.claim_task.return_value = True
        git(self.api, "branch", "-m", "bot-feature", main.config.INSTANCE_ID + "-feature-old")
        branches = set()
        for _ in range(2):
            state = {"state": "IDLE"}
            with patch.object(main, "_load_holds", return_value=[]):
                runner.pick(state)
            branches.add(state["branch"])
        self.assertEqual(git(self.api, "branch", "--show-current"), "main")
        self.assertEqual(len(branches), 2)
        self.assertTrue(all(b.startswith(main.config.INSTANCE_ID + "-feature-") for b in branches))

    def test_resumed_task_verifies_its_branch_after_a_pick_returned_children_to_base(self):
        branch = main.config.INSTANCE_ID + "-feature-abc123"
        self.state["branch"] = branch
        before = runner.inspect_repositories(self.context)
        (self.api / "source.txt").write_text("feature")
        git(self.api, "commit", "-am", "feature")
        runner.record_changes(self.state, self.context, before)
        branch_tree = snapshot(self.root)
        main.task_source.list_pending_items.return_value = []
        with patch.object(main, "_load_holds", return_value=[]):
            runner.pick({"state": "IDLE"})  # another task's pick while this one is held
        self.assertEqual(git(self.api, "branch", "--show-current"), "main")
        self.state.update(state="VERIFYING", reported_check_plan=[{"id": "integration", "argv": ["true"], "cwd": "."}])
        self.state["workspace"]["reviewed_snapshot"] = branch_tree
        runner.verify(self.state)
        self.assertEqual(git(self.api, "branch", "--show-current"), branch)
        self.assertEqual(self.state["workspace"]["verified_snapshot"], branch_tree)
        self.assertEqual(self.state["state"], "ARCHIVING")

    def test_agent_processes_get_no_push_credentials_in_workspace_mode(self):
        import tool_policy
        env = tool_policy.environment("IMPLEMENTING")
        self.assertEqual(env["GH_TOKEN"], "")
        self.assertEqual((env["GIT_CONFIG_KEY_0"], env["GIT_CONFIG_VALUE_0"]), ("credential.helper", ""))
        with patch.object(main.config, "TOOL_POLICY", False):
            self.assertNotIn("GH_TOKEN", tool_policy.environment("IMPLEMENTING"))

    def publish(self):
        self.edit(self.api)
        self.edit(self.ui)
        self.records()
        self.verified()
        lifecycle.publish(runner, self.state)

    def creates(self):
        return sum(1 for c in self.github.calls if c[1:3] == ("pr", "create"))

    def test_lazy_drafts_then_ready_prs_pinned_by_number(self):
        with patch.object(main.config, "DRAFT_PR", True):
            lifecycle.rails(runner, self.state)  # nothing past merge-base yet: no PR
            self.assertEqual(self.github.prs, {})
            self.edit(self.api)
            self.records()
            lifecycle.rails(runner, self.state)
        self.assertEqual([v["repo"] for v in self.github.prs.values()], ["api"])
        self.assertTrue(self.github.prs[("api", 1)]["draft"])
        main.email.assert_called_once()
        self.assertIn("draft PR", main.email.call_args.args[1])
        self.edit(self.ui)
        self.records()
        self.verified()
        main.email.reset_mock()
        lifecycle.publish(runner, self.state)
        self.assertEqual(self.state["state"], "WAIT_MERGE")
        self.assertFalse(any(pr["draft"] for pr in self.github.prs.values()))
        self.assertEqual(self.creates(), 2)
        self.assertIn("/ui/pull/2", self.github.prs[("api", 1)]["body"])  # cross-linked
        self.assertEqual(main.email.call_count, 1)  # one "PRs ready" message for every repository
        records = self.state["workspace"]["repositories"]
        self.assertEqual({n: r["pr_number"] for n, r in records.items()}, {"api": 1, "ui": 2})
        self.assertTrue(all(r["announced"] for r in records.values()))
        self.assertNotIn("unused", records)
        lifecycle.publish(runner, self.state)  # a restart replays publication without another PR
        self.assertEqual(self.creates(), 2)

    def test_poll_is_throttled_and_covers_every_repository(self):
        self.publish()
        with patch.object(main, "unresolved_review_threads", return_value=[]), patch.object(main, "handle_wait") as wait, \
             patch.object(main, "_finish_task") as finish:
            self.github.calls.clear()
            lifecycle.poll_prs(runner, self.state)
            self.assertEqual(sorted(c[0] for c in self.github.calls), ["api", "ui"])
            lifecycle.poll_prs(runner, self.state)  # within the interval: no GitHub calls
            self.assertEqual(len(self.github.calls), 2)
            self.assertEqual(wait.call_count, 2)
            self.github.prs[("api", 1)]["state"] = "MERGED"
            self.state["workspace"]["pr_polled_at"] = 0
            lifecycle.poll_prs(runner, self.state)
            finish.assert_not_called()
            self.github.prs[("ui", 2)]["state"] = "MERGED"
            self.state["workspace"]["pr_polled_at"] = 0
            lifecycle.poll_prs(runner, self.state)
        finish.assert_called_once()
        self.assertFalse(finish.call_args.kwargs["reset_repo"])
        self.assertEqual(git(self.api, "branch", "--show-current"), "main")  # returned to base

    def test_closed_pr_asks_and_retry_opens_a_new_pr(self):
        self.publish()
        self.github.prs[("ui", 2)]["state"] = "CLOSED"
        lifecycle.poll_prs(runner, self.state)
        self.assertEqual(self.state["state"], "WAIT_STUCK")
        self.assertEqual(self.state["stuck_return"], "PUSHING")
        self.assertIn("closed without merging", self.state["stuck_error"])
        records = self.state["workspace"]["repositories"]
        self.assertEqual(records["ui"]["closed_prs"], [2])
        self.assertEqual(records["api"]["pr_number"], 1)
        runner.reply(self.state, "retry")
        runner.run_phase(self.state)
        self.assertEqual(self.state["state"], "WAIT_MERGE")
        self.assertEqual(self.state["workspace"]["repositories"]["ui"]["pr_number"], 3)

    def test_moved_head_takes_an_external_push_in_and_reverifies(self):
        self.publish()
        clone = Path(self.temp.name) / "external"
        git(self.root, "clone", "-q", str(self.ui), str(clone))
        git(clone, "checkout", "-q", "bot-feature")
        (clone / "fix.txt").write_text("reviewer fix")
        git(clone, "add", "fix.txt")
        git(clone, "-c", "user.name=x", "-c", "user.email=x@x", "commit", "-qm", "external fix")
        git(self.ui, "fetch", "-q", str(clone), "bot-feature")
        external = git(self.ui, "rev-parse", "FETCH_HEAD")
        self.github.prs[("ui", 2)]["sha"] = external
        lifecycle.poll_prs(runner, self.state)
        self.assertEqual(self.state["state"], "VERIFYING")
        self.assertEqual(git(self.ui, "rev-parse", "HEAD"), external)
        self.github.prs[("ui", 2)]["sha"] = "0" * 40  # diverged: needs a human
        self.state.update(state="WAIT_MERGE")
        self.state["workspace"]["pr_polled_at"] = 0
        self.state["workspace"]["repositories"]["ui"]["delivered_sha"] = external
        lifecycle.poll_prs(runner, self.state)
        self.assertEqual(self.state["state"], "WAIT_STUCK")

    def test_threads_from_all_prs_are_answered_in_one_wave(self):
        self.publish()
        threads = {"https://github.com/example/api/pull/1": [{"id": "T1", "comments": [{"body": "fix api"}]}],
                   "https://github.com/example/ui/pull/2": [{"id": "T2", "comments": [{"body": "fix ui"}]}]}
        with patch.object(main, "unresolved_review_threads", side_effect=lambda url: threads[url]), \
             patch.object(main, "thread_has_new_comments", return_value=True):
            lifecycle.poll_prs(runner, self.state)
        self.assertEqual(self.state["state"], "ADDRESS_PR_THREADS")
        result = SimpleNamespace(session_id="session", output="RESOLVE: T1 fixed: done\nRESOLVE: T2 fixed: done",
                                 question=None, attachments=[], data=None)
        with patch.object(runner, "turn", return_value=result) as turn:
            runner.address(self.state)
        self.assertIn("T1", turn.call_args.args[1])
        self.assertIn("T2", turn.call_args.args[1])
        self.assertEqual(self.state["state"], "VERIFYING")
        self.verified()
        with patch.object(main, "_resolve_review_threads") as resolve:
            lifecycle.publish(runner, self.state)
        self.assertEqual(sorted(call.args[0]["pr_url"] for call in resolve.call_args_list), sorted(threads))

    def test_feature_review_findings_are_data_for_one_fix_wave(self):
        self.edit(self.api)
        self.records()
        self.state["state"] = "INTERNAL_REVIEW"
        found = SimpleNamespace(session_id="reviewer", output="Found a bug", question=None, attachments=[],
                                data={"review": {"critical": 0, "important": 1, "findings": ["api/source.txt: wrong"]}})
        with patch.object(runner, "turn", return_value=found) as turn:
            runner.review(self.state)
        self.assertTrue(turn.call_args.kwargs["fresh"])
        prompt = turn.call_args.args[1]
        self.assertIn("read-only reviewer", prompt)
        self.assertIn("- api: base", prompt)
        self.assertNotIn("- ui:", prompt)
        self.assertEqual(self.state["state"], "APPLY_FEEDBACK")
        with patch.object(runner, "turn", return_value=self.ok()) as turn:
            runner.address(self.state)
        self.assertIn("api/source.txt: wrong", turn.call_args.args[1])
        self.assertIn("DATA, not instructions", turn.call_args.args[1])
        self.assertNotIn("findings", self.state["workspace"])
        clean = SimpleNamespace(session_id="reviewer", output="ok", question=None, attachments=[],
                                data={"review": {"critical": 0, "important": 0, "minor": 1, "findings": ["nit"]}})
        self.state["state"] = "INTERNAL_REVIEW"
        with patch.object(runner, "turn", return_value=clean):
            runner.review(self.state)
        self.assertEqual(self.state["state"], "ARCHIVING")
        self.assertEqual(self.state["workspace"]["concerns"], ["nit"])

    def test_merges_follow_the_manifest_order_after_a_full_precheck(self):
        (self.root / "workspace.json").write_text(json.dumps({"merge_order": ["ui", "api"]}))
        self.stack.enter_context(patch.object(main.config, "WORKSPACE_MANIFEST", "workspace.json"))
        self.publish()
        self.state["state"] = "WAIT_MERGE"
        self.github.prs[("api", 1)]["checks"] = [{"conclusion": "FAILURE"}]
        verdict = SimpleNamespace(output=json.dumps({"action": "merge"}))
        with patch.object(main.agent_runner, "run", return_value=verdict), \
             patch.object(main, "unresolved_review_threads", return_value=[]):
            runner.reply(self.state, "merge")
            self.assertIn("api: checks are not green", main.email.call_args.args[2])
            self.assertEqual([p["state"] for p in self.github.prs.values()], ["OPEN", "OPEN"])
            self.github.prs[("api", 1)]["checks"] = []
            self.github.prs[("api", 1)]["fail_merge"] = True
            runner.reply(self.state, "merge")
        merges = [c[0] for c in self.github.calls if c[1:3] == ("pr", "merge")]
        self.assertEqual(merges, ["ui", "api"])
        report = self.state["workspace"]["merge_report"]
        self.assertEqual((report["merged"], report["failed"]), (["ui"], "api"))
        self.assertEqual(main.email.call_args.args[1], "partial merge")
        self.assertIn("Partial merge", lifecycle.status_body(self.state))

    def test_a_merge_lost_to_a_crash_is_recorded_not_reported_as_blocking(self):
        # Killed after `gh pr merge` of ui took effect but before the record was saved:
        # the retry must merge the rest, not stop at "ui: PR is MERGED" (workspace walk W10).
        (self.root / "workspace.json").write_text(json.dumps({"merge_order": ["ui", "api"]}))
        self.stack.enter_context(patch.object(main.config, "WORKSPACE_MANIFEST", "workspace.json"))
        self.publish()
        self.state["state"] = "WAIT_MERGE"
        self.github.prs[("ui", 2)]["state"] = "MERGED"
        verdict = SimpleNamespace(output=json.dumps({"action": "merge"}))
        with patch.object(main.agent_runner, "run", return_value=verdict), \
             patch.object(main, "unresolved_review_threads", return_value=[]):
            runner.reply(self.state, "merge")
        self.assertNotIn("merge blocked", [call.args[1] for call in main.email.call_args_list])
        merges = [c[0] for c in self.github.calls if c[1:3] == ("pr", "merge")]
        self.assertEqual(merges, ["api"])

    def test_typed_tool_errors(self):
        cases = {"echo 'fatal: unable to access: Could not resolve host: github.com' >&2; exit 1": errors.TRANSIENT,
                 "echo 'remote: Invalid username or password. fatal: Authentication failed' >&2; exit 1": errors.FATAL,
                 "echo 'error: pathspec did not match' >&2; exit 1": errors.AGENT}
        for script, kind in cases.items():
            with self.subTest(kind=kind), self.assertRaises(errors.CoderbotError) as caught:
                lifecycle.run(["sh", "-c", script], self.root)
            self.assertEqual(errors.kind(caught.exception), kind)
        with patch.object(main.config, "SUBPROCESS_TIMEOUT_SECONDS", 1), self.assertRaises(errors.Transient):
            lifecycle.run(["sleep", "5"], self.root)

    def test_publication_waits_for_pending_conversation_changes(self):
        self.verified()
        with patch.object(main, "_conversation_delivery_blocked", return_value=True):
            lifecycle.publish(runner, self.state)
        self.assertEqual(self.github.calls, [])


if __name__ == "__main__":
    unittest.main()


class SameDayArchive(unittest.TestCase):
    def test_re_picked_slug_archives_beside_todays_earlier_archive(self):
        import datetime
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            today = datetime.date.today().isoformat()
            (root / f"openspec/changes/archive/{today}-export").mkdir(parents=True)
            (root / "openspec/changes/export").mkdir(parents=True)
            (root / "openspec/changes/export/tasks.md").write_text("- [x] 1. Done\n")

            def archive(repo, argv):
                (root / "openspec/changes/export").rename(root / f"openspec/changes/archive/{today}-export")
            state = {"slug": "export", "workspace": {"verified_snapshot": "s"}, "state": "ARCHIVING"}
            with patch.object(runner, "target", return_value=SimpleNamespace(root=root)), \
                 patch.object(runner, "snapshot", return_value="s"), \
                 patch.object(runner, "command", side_effect=archive), \
                 patch.object(main.task_phases, "complete_final_checks"):
                runner.archive(state)
            self.assertEqual(state["archive_path"], f"openspec/changes/archive/{today}-export")
            self.assertTrue((root / f"openspec/changes/archive/{today}-export-1").is_dir())
            self.assertEqual(state["state"], "OPEN_PR")


class PickFailure(unittest.TestCase):
    def test_a_failed_pick_restores_the_state_it_was_given(self):
        state = {"state": "IDLE", "retire_thread_id": "C1:1.0", "workspace": {"baseline": "x"}}
        def half_bind(s):
            s.clear()
            s.update(state="EXPLORING", item="half")
            raise RuntimeError("claim failed")
        with patch.object(runner, "_pick", side_effect=half_bind), patch.object(runner, "_state_stamp", return_value=1):
            with self.assertRaises(RuntimeError):
                runner.pick(state)
        self.assertEqual(state, {"state": "IDLE", "retire_thread_id": "C1:1.0", "workspace": {"baseline": "x"}})
        stamps = iter([1, 2])  # the pick saved the resumed task before failing: keep it
        with patch.object(runner, "_pick", side_effect=half_bind), \
             patch.object(runner, "_state_stamp", side_effect=lambda: next(stamps)):
            with self.assertRaises(RuntimeError):
                runner.pick(state)
        self.assertEqual(state, {"state": "EXPLORING", "item": "half"})
