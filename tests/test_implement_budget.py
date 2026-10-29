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
