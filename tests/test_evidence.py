"""Regression tests for evidence collection input and recording wiring."""
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import evidence


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        p = patch("evidence.snapshot", return_value="fixture")
        p.start()
        self.addCleanup(p.stop)
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        p = patch.object(evidence.config, "DATA_DIR", Path(self.scratch.name))
        p.start()
        self.addCleanup(p.stop)

    def test_failed_validation_preserves_recorded_clips_and_exact_stage(self):
        import json
        with tempfile.TemporaryDirectory() as root:
            results = Path(root) / "test-results"
            results.mkdir()
            def run(cmd, **kwargs):
                (results / "demo.webm").write_bytes(b"video")
                return subprocess.CompletedProcess(cmd, 1, "4 passed", "E2E validation failed")
            with patch.object(evidence, "E2E_DIR", Path(root)), patch("evidence.operations.run", side_effect=run):
                self.assertEqual(evidence._record_playwright_video(["a.spec.ts"]), [])
            self.assertTrue((results / "demo.webm").exists())
            diagnostic = next(Path(self.scratch.name).rglob("diagnostic.json"))
            self.assertEqual(json.loads(diagnostic.read_text())["stage"], "validation")

    def test_media_probe_rejects_missing_duration_and_audio_only(self):
        import json
        video = Path(self.scratch.name) / "video.mp4"
        video.write_bytes(b"bytes")
        for value, expected in (({"format": {"duration": "2"}, "streams": [{"codec_type": "video"}]}, True),
                                ({"format": {"duration": "0"}, "streams": [{"codec_type": "video"}]}, False),
                                ({"format": {"duration": "2"}, "streams": [{"codec_type": "audio"}]}, False)):
            with self.subTest(value=value), patch("evidence.shutil.which", return_value="ffprobe"), \
                 patch("evidence.operations.run", return_value=subprocess.CompletedProcess([], 0, json.dumps(value), "")):
                self.assertEqual(evidence.valid_media(video), expected)

    def test_supplied_artifact_requires_current_snapshot_and_hash(self):
        import artifact_manifest
        outbox = Path(self.scratch.name) / "outbox"
        outbox.mkdir()
        video = outbox / "video.webm"
        video.write_bytes(b"bytes")
        artifact_manifest.write(outbox / "evidence-manifest.json", run_id="run", snapshot="fixture", status="pass",
                                artifacts=[{"path": str(video)}])
        with patch("evidence.valid_media", return_value=True):
            self.assertEqual(evidence.supplied_artifacts([video], "fixture"), [video.resolve()])
            self.assertEqual(evidence.supplied_artifacts([video], "different"), [])
            video.write_bytes(b"modified")
            self.assertEqual(evidence.supplied_artifacts([video], "fixture"), [])

    def test_timeout_bytes_and_ffmpeg_timeout_are_best_effort(self):
        with patch("evidence.operations.run", side_effect=subprocess.TimeoutExpired(["run"], 1,
                 output=b"partial", stderr=b"error")), patch("evidence._teardown_stack"):
            passed, output = evidence.run_suite()
        self.assertFalse(passed)
        self.assertIn("partial", output)
        with tempfile.TemporaryDirectory() as root:
            clip = Path(root) / "clip.webm"
            clip.write_bytes(b"video")
            with patch("evidence.shutil.which", return_value="ffmpeg"), \
                 patch.object(evidence.config, "DATA_DIR", Path(root)), \
                 patch("evidence.operations.run", side_effect=subprocess.TimeoutExpired(["ffmpeg"], 1)):
                self.assertIsNone(evidence._stitch_to_mp4([clip]))
    def test_old_clip_is_not_returned_when_recording_produces_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            e2e = Path(tmp)
            (e2e / "test-results").mkdir()
            (e2e / "test-results/old.webm").write_bytes(b"old")
            with patch.object(evidence, "E2E_DIR", e2e), \
                 patch.object(evidence.config, "DATA_DIR", e2e), \
                 patch("evidence.operations.run", return_value=subprocess.CompletedProcess([], 0, "", "")):
                self.assertEqual(evidence._record_playwright_video(["a.spec.ts"]), [])
    def test_reported_specs_strip_markdown_code_delimiters(self):
        output = "summary\nE2E_SPEC: `dynamic-page-title.spec.ts`\n"

        self.assertEqual(evidence.reported_specs(output), ["dynamic-page-title.spec.ts"])

    def test_recorder_normalizes_persisted_markdown_spec_name(self):
        with patch("evidence._record_playwright_video", return_value=[]) as record, \
             patch("evidence.detect_branch_specs", return_value=[]), \
             patch("evidence.snapshot", return_value="fixture"), \
             tempfile.TemporaryDirectory() as root, patch.object(evidence.config, "DATA_DIR", Path(root)):
            evidence.record_evidence(["dynamic-page-title.spec.ts`"], "playwright")

        self.assertEqual(record.call_args.args[0], ["dynamic-page-title.spec.ts"])

    def test_playwright_recorder_uses_generic_video_environment_variable(self):
        with tempfile.TemporaryDirectory() as tmp:
            e2e_dir = Path(tmp)
            with patch.object(evidence, "E2E_DIR", e2e_dir), \
                 patch("evidence.operations.run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
                evidence._record_playwright_video(["dynamic-page-title.spec.ts"])

        # @evidence demo test first, then the full spec when no clip appeared.
        commands = [c.args[0] for c in run.call_args_list]
        self.assertEqual(commands, [
            ["./run.sh", "dynamic-page-title.spec.ts", "--grep", "@evidence"],
            ["./run.sh", "dynamic-page-title.spec.ts"]])
        self.assertEqual(run.call_args.kwargs["env"]["PW_VIDEO"], "on")

    def test_recording_preserves_target_environment_without_application_defaults(self):
        with patch.dict(evidence.os.environ, {"TARGET_SETTING": "custom"}, clear=True), \
             patch("evidence.operations.run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            evidence._run_recording(["a.spec.ts"], [])
        self.assertEqual(run.call_args.kwargs["env"], {"TARGET_SETTING": "custom", "PW_VIDEO": "on"})

    def test_external_artifacts_are_reused_through_an_explicit_manifest(self):
        import artifact_manifest
        root = Path(self.scratch.name)
        video = root / "custom-results/demo.webm"
        video.parent.mkdir()
        video.write_bytes(b"video")
        manifest = root / "custom-results/artifacts.json"
        artifact_manifest.write(manifest, run_id="run", snapshot="fixture", status="pass",
                                artifacts=[{"path": str(video)}])
        with patch.dict(evidence.os.environ, {"CODEBOT_ARTIFACT_MANIFEST": str(manifest)}), \
             patch("evidence.detect_branch_specs", return_value=[]), \
             patch("evidence.valid_media", return_value=True), \
             patch("evidence._run_recording") as run:
            self.assertEqual(evidence.record_evidence(["a.spec.ts"], "playwright"), [video.resolve()])
        run.assert_not_called()

    def test_evidence_clip_stops_after_demo_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            e2e_dir = Path(tmp)
            results = e2e_dir / "test-results"
            results.mkdir()

            def fake_run(cmd, **kwargs):
                (results / "demo.webm").write_bytes(b"video")
                import json
                (e2e_dir / "results.json").write_text(json.dumps({"stats": {"expected": 1}, "errors": [],
                    "suites": [{"specs": [{"file": "a.spec.ts", "title": "@evidence demo", "tests": [{"status": "expected", "expectedStatus": "passed",
                        "results": [{"status": "passed", "retry": 0, "errors": [], "attachments": [
                            {"contentType": "video/webm", "path": str(results / "demo.webm")}]}]}]}]}]}))
                return subprocess.CompletedProcess(cmd, 0, "", "")

            with patch.object(evidence, "E2E_DIR", e2e_dir), \
                  patch("evidence.operations.run", side_effect=fake_run) as run, \
                  patch("evidence.valid_media", return_value=True), \
                  patch("evidence._stitch_to_mp4", return_value=None):
                clips = evidence._record_playwright_video(["a.spec.ts"])

        self.assertEqual(run.call_count, 1)
        self.assertEqual([c.name for c in clips], ["demo.webm"])

    def test_rejected_demo_generates_and_validates_complete_spec_before_conversion(self):
        import json
        e2e_dir = Path(self.scratch.name) / "e2e"
        results = e2e_dir / "test-results"
        results.mkdir(parents=True)
        calls = []

        def run(cmd, **kwargs):
            calls.append(cmd)
            clip = results / ("partial.webm" if len(calls) == 1 else "complete.webm")
            clip.write_bytes(b"video")
            (e2e_dir / "results.json").write_text(json.dumps({"stats": {"expected": 1}, "errors": [],
                "suites": [{"specs": [{"file": "a.spec.ts", "title": "@evidence demo", "tests": [{"status": "expected", "expectedStatus": "passed",
                    "results": [{"status": "passed", "retry": 0, "errors": [], "attachments": [
                        {"contentType": "video/webm", "path": str(clip)}]}]}]}]}]}))
            return subprocess.CompletedProcess(cmd, 1 if len(calls) == 1 else 0, "1 passed", "inventory mismatch" if len(calls) == 1 else "")

        with patch.object(evidence, "E2E_DIR", e2e_dir), \
             patch("evidence.operations.run", side_effect=run), \
             patch("evidence.valid_media", return_value=True), \
             patch("evidence._stitch_to_mp4", return_value=None) as stitch:
            clips = evidence._record_playwright_video(["a.spec.ts"])
        self.assertEqual(calls, [["./run.sh", "a.spec.ts", "--grep", "@evidence"], ["./run.sh", "a.spec.ts"]])
        self.assertEqual(clips, [results / "complete.webm"])
        self.assertEqual(stitch.call_args.args[0], clips)
        self.assertTrue((results / "partial.webm").exists())

    def test_passing_full_suite_without_demo_for_every_spec_requires_implementation(self):
        import json
        e2e_dir = Path(self.scratch.name) / "e2e"
        results = e2e_dir / "test-results"
        results.mkdir(parents=True)
        def run(cmd, **kwargs):
            clip = results / "demo.webm"
            clip.write_bytes(b"video")
            (e2e_dir / "results.json").write_text(json.dumps({"stats": {"expected": 1}, "errors": [],
                "suites": [{"specs": [{"file": "a.spec.ts", "title": "@evidence demo", "tests": [{"status": "expected", "expectedStatus": "passed",
                    "results": [{"status": "passed", "retry": 0, "errors": [], "attachments": [
                        {"contentType": "video/webm", "path": str(clip)}]}]}]}]}]}))
            return subprocess.CompletedProcess(cmd, 0, "1 passed", "")
        with patch.object(evidence, "E2E_DIR", e2e_dir), \
             patch("evidence.operations.run", side_effect=run), \
             patch("evidence.valid_media", return_value=True), patch("evidence._stitch_to_mp4") as stitch:
            self.assertEqual(evidence._record_playwright_video(["a.spec.ts", "missing.spec.ts"], full_suite=True), [])
        stitch.assert_not_called()
        diagnostic = json.loads(next(Path(self.scratch.name).rglob("diagnostic.json")).read_text())
        self.assertEqual(diagnostic["stage"], "implementation")
        self.assertIn("missing.spec.ts", diagnostic["detail"])

    def test_suite_timeout_tears_down_stack_and_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            e2e_dir = Path(tmp)
            (e2e_dir / "docker-compose.e2e.yaml").write_text("services: {}\n")
            calls = []

            def fake_run(cmd, **kwargs):
                calls.append(cmd)
                if cmd[0] == "./run.sh":
                    raise subprocess.TimeoutExpired(cmd, 1, output="partial", stderr="")
                return subprocess.CompletedProcess(cmd, 0, "", "")

            with patch.object(evidence, "E2E_DIR", e2e_dir), \
                 patch.object(evidence.config, "E2E_TIMEOUT_SECONDS", 1), \
                 patch("evidence.subprocess.run", side_effect=fake_run), \
                 patch("evidence.operations.run", side_effect=fake_run):
                passed, output = evidence.run_suite()

        self.assertFalse(passed)
        self.assertIn("TIMED OUT", output)
        self.assertIn("partial", output)
        self.assertEqual(calls[1][:4], ["docker", "compose", "-f", "docker-compose.e2e.yaml"])

    def test_teardown_is_noop_without_compose_file(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(evidence, "E2E_DIR", Path(tmp)), \
             patch("evidence.subprocess.run") as run:
            evidence._teardown_stack()
        run.assert_not_called()

    def test_stitched_video_lands_in_data_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            data_dir.mkdir()
            clip = Path(tmp) / "a.webm"
            clip.write_bytes(b"video")

            def fake_ffmpeg(cmd, **kwargs):
                Path(cmd[-1]).write_bytes(b"mp4")
                return subprocess.CompletedProcess(cmd, 0, "", "")

            with patch.object(evidence.config, "DATA_DIR", data_dir), \
                  patch("evidence.shutil.which", return_value="/usr/bin/ffmpeg"), \
                  patch("evidence.valid_media", side_effect=lambda p: Path(p).is_file()), \
                  patch("evidence.operations.run", side_effect=fake_ffmpeg):
                out = evidence._stitch_to_mp4([clip])

            self.assertTrue(out.is_relative_to(data_dir / "outbox/evidence"))
            self.assertEqual(out.read_bytes(), b"mp4")

    def test_agent_playwright_clips_are_stitched(self):
        with tempfile.TemporaryDirectory() as tmp:
            clips = [Path(tmp) / "first.webm", Path(tmp) / "second.webm"]
            for clip in clips:
                clip.write_bytes(b"video")
            expected = Path(tmp) / "evidence.mp4"
            with patch("evidence._stitch_to_mp4", return_value=expected) as stitch:
                self.assertEqual(evidence.stitch_playwright_clips(clips), expected)

        self.assertEqual(stitch.call_args.args[0], clips)


if __name__ == "__main__":
    unittest.main()
