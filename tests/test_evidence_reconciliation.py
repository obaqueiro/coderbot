import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

import artifact_manifest
import evidence
import evidence_delivery


class ReconciliationTests(unittest.TestCase):
    def test_arbitrary_pr_link_without_attestation_is_not_adopted(self):
        with tempfile.TemporaryDirectory() as root, patch.object(evidence_delivery.config, "DATA_DIR", Path(root)), \
             patch.object(evidence_delivery.drive_client, "verify_existing") as verify:
            self.assertIsNone(evidence_delivery.reconcile({"pr_url": "pr"}, root, "https://drive.google.com/file/d/a/view"))
            verify.assert_not_called()

    def test_archiving_preserves_identity_but_code_change_invalidates_it(self):
        import subprocess
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            (root / "app.py").write_text("code")
            planning = root / "openspec/changes/demo"
            planning.mkdir(parents=True)
            (planning / "tasks.md").write_text("tasks")
            before = evidence_delivery.implementation_snapshot(root)
            (planning / "tasks.md").write_text("completed tasks")
            self.assertEqual(before, evidence_delivery.implementation_snapshot(root))
            (root / "app.py").write_text("changed code")
            self.assertNotEqual(before, evidence_delivery.implementation_snapshot(root))

    def test_attested_published_video_is_adopted_without_recording(self):
        import main
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            video = root / "outbox/demo/video.mp4"
            video.parent.mkdir(parents=True)
            video.write_bytes(b"video")
            url = "https://drive.google.com/file/d/a/view"
            manifest = root / "outbox/evidence/run/verified-delivery.json"
            manifest.parent.mkdir(parents=True)
            artifact_manifest.write(manifest, run_id="run", snapshot="implementation", status="pass", artifacts=[{"path": str(video)}])
            metadata = json.loads(manifest.read_text())
            metadata["pr_url"] = "https://github.com/a/b/pull/1"
            manifest.write_text(json.dumps(metadata))
            (video.parent / "drive-entrega.json").write_text(json.dumps({"file": {"id": "a", "webViewLink": url}, "folder_id": "folder", "sha256": artifact_manifest.file_hash(video)}))
            state = {"item": "task", "slug": "task", "state": "WAIT_MERGE", "pr_url": metadata["pr_url"], "has_e2e_harness": True}
            with patch.object(main.config, "DATA_DIR", root), patch.object(main, "content_snapshot", return_value="snapshot"), \
                 patch("evidence_delivery.implementation_snapshot", return_value="implementation"), \
                 patch("evidence.valid_media", return_value=True), \
                 patch("drive_client.verify_existing", return_value={"status": "complete", "url": url}) as verify, \
                 patch.object(main.lifecycle.RepoOps, "gh", return_value=json.dumps({"body": f"Demo: {url}"})), \
                 patch.object(main.evidence, "record_evidence") as record, \
                 patch.object(main, "email", return_value=None) as notify, patch.object(main, "save_state"), \
                 patch.object(main, "_publish_progress"), patch.object(main, "unresolved_review_threads", return_value=[]):
                main.finalize_pr(state)
                record.assert_not_called()
                verify.assert_called_once()
                self.assertIn(url, notify.call_args.args[2])
                self.assertNotIn("no approved e2e evidence", notify.call_args.args[2])

    def test_docker_path_maps_only_to_current_run(self):
        with tempfile.TemporaryDirectory() as root:
            report = Path(root) / "results.json"
            self.assertEqual(evidence.report_video_path(report, "/results/test-results/demo/video.webm"),
                             (report.parent / "test-results/demo/video.webm").resolve())
            for bad in ("/results/../secret", "/etc/passwd", "/results/html/video.webm"):
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    evidence.report_video_path(report, bad)

    def test_stale_attestation_is_not_adopted(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            manifest = root / "outbox/evidence/run/verified-delivery.json"
            manifest.parent.mkdir(parents=True)
            video = root / "old.mp4"
            video.write_bytes(b"old")
            artifact_manifest.write(manifest, run_id="run", snapshot="old", status="pass", artifacts=[{"path": str(video)}])
            value = json.loads(manifest.read_text())
            value["pr_url"] = "pr"
            manifest.write_text(json.dumps(value))
            with patch.object(evidence_delivery.config, "DATA_DIR", root), \
                 patch("evidence_delivery.implementation_snapshot", return_value="current"), \
                 patch("drive_client.verify_existing") as verify:
                self.assertIsNone(evidence_delivery.reconcile({"pr_url": "pr"}, root, "link"))
                verify.assert_not_called()
