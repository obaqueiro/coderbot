"""Video links update PR metadata without replacing human-authored description."""
import json
import sys
import unittest
from unittest.mock import Mock, patch

import markdown
with patch.dict(sys.modules, {"gdoc_client": Mock(), "task_source": Mock(), "gmail_client": Mock()}):
    import main


class PRVideoTests(unittest.TestCase):
    def test_appends_video_preserving_description_and_updates_summary(self):
        state = {"pr_url": "https://github.com/org/repo/pull/1"}
        current = "Human summary\n\nCloses #7"
        def command(*argv):
            nonlocal current
            if argv[1] == "edit":
                current = argv[-1]
                return ""
            return json.dumps({"body": current})
        with patch.object(main.lifecycle.RepoOps, "gh", side_effect=command) as run:
            main._sync_pr_video(state, "https://drive.google.com/file/d/first/view")
        body = current
        self.assertTrue(body.startswith("Human summary\n\nCloses #7"))
        self.assertIn("[Video](https://drive.google.com/file/d/first/view)", body)
        self.assertEqual(state["pr_summary"], body)

    def test_replaces_existing_video_and_identical_link_is_idempotent(self):
        state = {"pr_url": "https://github.com/org/repo/pull/1"}
        old = "Summary\n\n<!-- coderbot:video:start -->\nOld video\n<!-- coderbot:video:end -->\nHuman footer"
        current = old
        def command(*argv):
            nonlocal current
            if argv[1] == "edit":
                current = argv[-1]
                return ""
            return json.dumps({"body": current})
        with patch.object(main.lifecycle.RepoOps, "gh", side_effect=command) as run:
            main._sync_pr_video(state, "https://drive.google.com/file/d/new/view")
        body = current
        self.assertNotIn("Old video", body)
        self.assertTrue(body.endswith("Human footer"))
        with patch.object(main.lifecycle.RepoOps, "gh", return_value=json.dumps({"body": body})) as run:
            main._sync_pr_video(state, "https://drive.google.com/file/d/new/view")
        self.assertEqual(run.call_count, 2)
