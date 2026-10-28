import unittest
from unittest.mock import patch
from types import SimpleNamespace

import review_intent
import main


class ReviewIntentTests(unittest.TestCase):
    def test_agent_receives_approved_intent_constraint_for_conflicting_default(self):
        state = {"item": "High effort default", "state": "ADDRESS_PR_THREADS", "session_id": "s",
                 "branch": "feature", "pr_threads": [{"body": "Use medium effort instead"}]}
        with patch.object(main, "_render_review_threads", return_value="Approved: high. Finding: use medium."), \
             patch.object(main.agent_runner, "resume", return_value=SimpleNamespace(output="explained", session_id="s", question=None)) as agent, \
             patch.object(main, "handle_result", return_value=False), \
             patch.object(main, "_scrub_evidence_from_repo", return_value=[]), \
             patch.object(main, "_queue_push"):
            main.do_address_pr_threads(state)
        prompt = agent.call_args.args[1]
        self.assertIn("Do not reverse approved defaults solely to close threads", prompt)
        self.assertIn("design conflict", prompt)

    def test_provenance_requires_actual_evidence(self):
        self.assertEqual(review_intent.provenance({"author": "alice", "author_type": "User"}), "human")
        self.assertEqual(review_intent.provenance({"author": "reviewer", "author_type": "Bot"}), "automated")
        self.assertEqual(review_intent.provenance({"author": "unknown"}), "unknown")
        self.assertEqual(review_intent.provenance({"author": "custom-reviewer[bot]"}), "automated")
        self.assertEqual(review_intent.provenance({"author": "custom-reviewer", "author_type": "User"}), "human")
        self.assertEqual(review_intent.provenance({"author": "custom-reviewer"}), "unknown")
