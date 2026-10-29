"""Implementation turns are bounded in code: sections + 3, one fresh session, then ask."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

with patch.dict(sys.modules, {"gdoc_client": Mock(), "task_source": Mock(), "gmail_client": Mock()}):
    import main


class ImplementBudget(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        tasks = self.root / "openspec/changes/task/tasks.md"
        tasks.parent.mkdir(parents=True)
        tasks.write_text("## 1. Model\n- [x] 1.1 a\n## 2. API\n- [ ] 2.1 b\n")
        for patcher in (patch.object(main.config, "REPO_PATH", self.root), patch.object(main, "save_state"),
                        patch.object(main, "trail"), patch.object(main, "_scrub_evidence_from_repo", return_value=[]),
                        patch.object(main, "content_snapshot", return_value="same"),
                        patch.object(main, "_run_checked", return_value=json.dumps(
                            {"state": "ready", "progress": {"total": 2, "complete": 1, "remaining": 1}}))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def turn(self, state):
        main._complete_implementation(state, SimpleNamespace(output="did work", attachments=[]))

    def test_budget_is_sections_plus_three_capped_at_twelve(self):
        self.assertEqual(main._implement_budget("task"), 5)
        (self.root / "openspec/changes/task/tasks.md").write_text("".join(f"## {n}. S\n" for n in range(1, 20)))
        self.assertEqual(main._implement_budget("task"), 12)
        self.assertEqual(main._implement_budget("missing"), 12)

    def test_exhausted_budget_gets_one_fresh_session_then_asks(self):
        state = {"state": "IMPLEMENTING", "slug": "task", "item": "task", "implement_budget": 2}
        with patch.object(main, "_gate_failed") as gate:
            self.turn(state)
            self.assertFalse(state.get("fresh_session"))
            with patch.object(main, "content_snapshot", return_value="changed"):
                self.turn(state)
            self.assertTrue(state["fresh_session"])
            self.assertEqual(state["fresh_retries"], 1)
            for n in range(main.IMPLEMENT_FRESH_TURNS):
                with patch.object(main, "content_snapshot", return_value=f"progress-{n}"):
                    self.turn(state)
        gate.assert_called_once()
        self.assertIn("turn budget", gate.call_args.args[3])

    def test_approval_hands_the_implementer_a_fresh_session(self):
        import agent_runner
        state = {"state": "IMPLEMENTING", "session_id": "planning", "fresh_session": True}
        token = agent_runner._task_context.set(state)
        self.addCleanup(agent_runner._task_context.reset, token)
        with patch.object(agent_runner.config, "AGENT", "claude"), \
             patch.object(agent_runner.phase_checkpoint, "replay", return_value=None), \
             patch.object(agent_runner.phase_checkpoint, "record"), \
             patch.object(agent_runner, "_persist"), \
             patch.object(agent_runner.claude_runner, "run", return_value=Mock(session_id="new", output="ok")) as run, \
             patch.object(agent_runner.claude_runner, "resume") as resume:
            agent_runner.resume("planning", "implement")
        run.assert_called_once()
        resume.assert_not_called()
        self.assertNotIn("fresh_session", state)


if __name__ == "__main__":
    unittest.main()


class ReviewLeftovers(unittest.TestCase):
    def result(self, **review):
        return SimpleNamespace(output="reviewed", attachments=[], data={"status": "done", "summary": "s", "review": review})

    def test_open_important_findings_proceed_and_are_listed(self):
        value = main._review_with_leftovers(self.result(critical=0, important=1, tests=["pytest: pass"],
                                                        findings=["api.py: missing retry"]))
        self.assertEqual(value["concerns"], ["api.py: missing retry"])
        self.assertIn("- api.py: missing retry", main._review_concerns({"internal_review_report": value}))

    def test_open_critical_findings_or_no_tests_still_gate(self):
        self.assertIsNone(main._review_with_leftovers(self.result(critical=1, important=0, tests=["t: pass"])))
        self.assertIsNone(main._review_with_leftovers(self.result(critical=0, important=2)))
        self.assertIsNone(main._review_with_leftovers(SimpleNamespace(output="x", data=None)))


class EvidenceScrub(unittest.TestCase):
    def test_committed_evidence_moves_to_the_outbox_without_an_agent_turn(self):
        import subprocess
        def run(cwd, *args):
            subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)
        with tempfile.TemporaryDirectory() as tmp:
            repo, data = Path(tmp) / "repo", Path(tmp) / "data"
            repo.mkdir()
            run(repo, "init", "-q", "-b", "main")
            run(repo, "config", "user.email", "t@t"); run(repo, "config", "user.name", "t")
            (repo / "README.md").write_text("x\n")
            run(repo, "add", "."); run(repo, "commit", "-qm", "base")
            run(repo, "checkout", "-qb", "feature")
            (repo / "src").mkdir()
            (repo / "src/evidence_parser.py").write_text("code\n")
            (repo / "demo.webm").write_bytes(b"video")
            (repo / "evidence-shot.png").write_bytes(b"png")
            run(repo, "add", "."); run(repo, "commit", "-qm", "feature")
            state = {"branch": "feature", "slug": "task", "session_id": "s"}
            with patch.object(main.config, "REPO_PATH", repo), patch.object(main.config, "DATA_DIR", data), \
                 patch.object(main.config, "BASE_BRANCH", "main"), patch.object(main, "trail"), \
                 patch.object(main.agent_runner, "resume") as resume:
                moved = main._scrub_evidence_from_repo(state, "IMPLEMENTING")
            resume.assert_not_called()
            self.assertEqual(sorted(p.name for p in moved), ["demo.webm", "evidence-shot.png"])
            self.assertTrue(all(p.is_file() for p in moved))
            tracked = subprocess.run(["git", "ls-files"], cwd=repo, capture_output=True, text=True).stdout.split()
            self.assertEqual(sorted(tracked), ["README.md", "src/evidence_parser.py"])
            self.assertFalse((repo / "demo.webm").exists())


class FeatureReviewWave(unittest.TestCase):
    def test_findings_get_one_fix_wave_then_one_scoped_re_review(self):
        state = {"state": "INTERNAL_REVIEW", "slug": "task", "item": "task", "session_id": "impl",
                 "base_sha": "base"}
        found = SimpleNamespace(output="review", attachments=[], session_id="rev", question=None,
                                data={"status": "done", "summary": "s", "review": {
                                    "critical": 1, "important": 0, "findings": ["api.py: no auth"]}})
        with patch.object(main, "git", return_value="head-1"), patch.object(main, "trail"), \
             patch.object(main, "content_snapshot", return_value="same"), \
             patch.object(main.agent_runner, "resume", return_value=found):
            main.do_internal_review(state)
        self.assertEqual((state["state"], state["review_fix_base"]), ("APPLY_FEEDBACK", "head-1"))
        fixed = SimpleNamespace(output="fixed", attachments=[], session_id="impl", question=None, data=None)
        with patch.object(main.agent_runner, "resume", return_value=fixed) as resume, \
             patch.object(main, "save_state"):
            main.do_apply_feedback(state)
        self.assertIn("api.py: no auth", resume.call_args.args[1])
        self.assertEqual(state["state"], "VERIFYING")
        prompt = main._internal_review_prompt(state)
        self.assertIn("Re-review only the fix commits, `git diff head-1...HEAD`", prompt)
        with patch.object(main, "git", return_value="head-2"), \
             patch.object(main, "content_snapshot", return_value="same"), \
             patch.object(main, "_gate_failed") as gate, \
             patch.object(main.agent_runner, "resume", return_value=found):
            main.do_internal_review(state)
        gate.assert_called_once()  # still critical after the wave: the gate, not another wave
        self.assertEqual(state["review_fix_base"], "head-1")

    def test_a_reviewer_that_edits_is_discarded(self):
        state = {"state": "INTERNAL_REVIEW", "slug": "task", "item": "task", "session_id": "impl"}
        snapshots = iter(["before", "after"])
        with patch.object(main, "git", return_value="head"), \
             patch.object(main, "content_snapshot", side_effect=lambda repo: next(snapshots)), \
             patch.object(main, "_preserve_uncommitted", return_value="stashed") as preserve, \
             patch.object(main, "_gate_failed") as gate, \
             patch.object(main.agent_runner, "resume", return_value=SimpleNamespace(output="x", data=None)):
            main.do_internal_review(state)
        preserve.assert_called_once()
        gate.assert_called_once()


class LargeReviewSplit(unittest.TestCase):
    def test_a_large_diff_is_reviewed_in_two_parts_then_one_fix_wave(self):
        numstat = "1500\t0\tsrc/a.py\n900\t100\tsrc/b.py\n10\t0\ttests/test_a.py"
        state = {"state": "INTERNAL_REVIEW", "slug": "task", "item": "task", "session_id": "s", "base_sha": "base"}
        def git(*args):
            return numstat if args[0] == "diff" else "head"
        def reviewed(findings):
            return SimpleNamespace(output="r", attachments=[], session_id="rev", question=None, data={
                "status": "done", "summary": "s", "review": {"critical": len(findings), "important": 0,
                                                             "findings": findings, "tests": ["t: pass"]}})
        prompts_seen = []
        with patch.object(main, "git", side_effect=git), patch.object(main, "trail"), \
             patch.object(main.config, "REVIEW_SPLIT_LINES", 2000), \
             patch.object(main, "content_snapshot", return_value="same"):
            for findings in (["src/a.py: bug"], []):
                with patch.object(main.agent_runner, "resume", return_value=reviewed(findings)) as resume:
                    main.do_internal_review(state)
                prompts_seen.append(resume.call_args.args[1])
        self.assertIn("part 1 of 2", prompts_seen[0])
        self.assertIn("-- src/a.py", prompts_seen[0])
        self.assertIn("part 2 of 2", prompts_seen[1])
        self.assertIn("src/b.py tests/test_a.py", prompts_seen[1])
        self.assertEqual(state["state"], "APPLY_FEEDBACK")
        self.assertEqual(state["review_findings"], ["src/a.py: bug"])
