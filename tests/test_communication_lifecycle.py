"""End-to-end communication handoff across the two supported transports."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

# Keep Markdown outside the temporary module registry. Otherwise patch.dict
# removes it after importing main, leaving proposal_package with a stale core
# whose Extension class differs from subsequently imported extension modules.
import markdown

with patch.dict(sys.modules, {"gdoc_client": Mock(), "task_source": Mock(),
                              "gmail_client": Mock()}):
    import main


class CommunicationLifecycle(unittest.TestCase):
    def test_review_markdown_extensions_share_the_renderer_module(self):
        self.assertIs(main.proposal_package.markdown, markdown)
        document = main.proposal_package.render({
            "design.md": "## Design\n\n```python\nprint('ok')\n```\n\n"
                         "| Check | Result |\n| --- | --- |\n| Tests | Passed |\n"
        }, "Review")
        self.assertIn("<pre><code>", document)
        self.assertIn("<table>", document)
        self.assertIn("Passed", document)

    def test_proposal_pr_and_merge_in_email_and_slack(self):
        for channel in ("email", "slack"):
            with self.subTest(channel=channel), tempfile.TemporaryDirectory() as tmp:
                repo = Path(tmp) / "repo"
                change = repo / "openspec/changes/example"
                (change / "specs/api").mkdir(parents=True)
                for name in ("proposal.md", "design.md", "tasks.md"):
                    (change / name).write_text(f"## {name}\nArchitecture\n")
                (change / "specs/api/spec.md").write_text("### Requirement: API\n")
                state = {"state": "EXPLORING", "item": "Create API", "slug": "example",
                         "branch": "bot-example", "base_sha": "a" * 40,
                         "archive_path": "openspec/changes/archive/2026-09-29-example",
                         "pr_url": "https://github.com/org/repo/pull/3",
                         "has_e2e_harness": False, "pr_summary": "Create endpoint",
                         "thread_id": "C1:1.0" if channel == "slack" else None}
                architecture = {"head": "b" * 40, "paths": ["deploy/database.tf"],
                                "planned": "## Design\nRDS", "diff": "+new DB", "complete": True}
                decision = {"decisions": [{"kind": "decision", "planned": False,
                                          "title": "Separate production RDS",
                                          "impact": "New database deployment boundary",
                                          "paths": ["deploy/database.tf"]}]}
                with patch.object(main.config, "REPO_PATH", repo), \
                     patch.object(main, "content_snapshot", return_value="fixture"), \
                     patch.object(main.config, "DATA_DIR", Path(tmp) / "data"), \
                     patch.object(main.config, "COMM_CHANNEL", channel), \
                     patch.object(main, "save_state"), patch.object(main, "trail"), \
                     patch.object(main.task_source, "mark_done", return_value=True), \
                     patch.object(main.gmail_client, "send", return_value=state["thread_id"] or "gmail-1") as send, \
                     patch.object(main.gmail_client, "deliver", side_effect=lambda s, subj, body, thread, files: (send(subj, body, thread, files), {"thread_id": thread or "gmail-1", "complete": True})[1]), \
                     patch.object(main.evidence, "record_evidence", return_value=[]), \
                     patch.object(main.lifecycle, "review_threads", return_value=[]), \
                     patch.object(main.agent_runner, "run",
                                  return_value=SimpleNamespace(output=json.dumps(decision))):
                    main.announce_milestone(state, "exploring", "Exploring")
                    state["state"] = "PROPOSING"
                    main.announce_milestone(state, "proposing", "Writing proposal")
                    main._send_proposal_review(state, "Summary")
                    main.announce_milestone(state, "implementing", "Building")
                    main.announce_milestone(state, "verifying", "Checking")
                    main.announce_milestone(state, "archiving", "Archiving")
                    main.finalize_pr(state)
                    self.assertEqual(state["state"], "WAIT_MERGE")
                    main._finish_task(state, "PR merged.", reset_repo=False, merged=True)
                bodies = [call.args[1] for call in send.call_args_list]
                self.assertEqual(bodies.count("Exploring"), 1)
                self.assertTrue(any("Review cover:" in body for body in bodies))
                self.assertTrue(any(call.args[3] and call.args[3][0].suffix == ".html"
                                    for call in send.call_args_list))
                stages = [call.kwargs["progress"].stem for call in send.call_args_list
                          if call.kwargs.get("progress")]
                self.assertEqual(stages, ["exploring", "proposing", "approval", "implementing",
                                          "verifying", "archiving", "pr_review", "merged"])
                self.assertEqual(state["state"], "IDLE")
                self.assertNotIn("milestones_announced", state)
