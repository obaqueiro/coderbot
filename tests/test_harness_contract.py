import json
from pathlib import Path
import tempfile
import unittest
from harness_contract import load


class HarnessTests(unittest.TestCase):
    def test_provider_groups_and_resources_are_declared_by_the_repository(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "e2e"
            path.mkdir()
            prep = {"inputs": ["e2e/package-lock.json"], "commands": [["prepare"]]}
            (path / "codebot-harness.json").write_text(json.dumps({"version": 1,
                "preparation": prep, "checks": [
                    {"id": "provider-a", "argv": ["./run.sh", "--provider", "provider-a", "a.spec.ts", "b.spec.ts"],
                     "cwd": "e2e", "resources": ["shared-harness"]},
                    {"id": "provider-b", "argv": ["./run.sh", "--provider", "provider-b", "c.spec.ts"],
                     "cwd": "e2e", "resources": ["shared-harness"]}]}))
            checks = load(root)
            self.assertEqual(len(checks), 2)
            self.assertEqual(checks[0].argv, ["./run.sh", "--provider", "provider-a", "a.spec.ts", "b.spec.ts"])
            self.assertEqual(checks[1].argv[2], "provider-b")
            self.assertEqual(checks[0].resources, checks[1].resources)
            self.assertTrue(all(check.preparation == prep for check in checks))
    def test_legacy_and_versioned_provider_plan(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertIsNone(load(root))
            path = Path(root) / "e2e"
            path.mkdir()
            (path / "codebot-harness.json").write_text(json.dumps({"version": 1,
                "preparation": {"commands": [["prepare"]], "probes": [["probe"]]},
                "checks": [{"id": "provider", "argv": ["./run.sh", "--provider", "provider"], "cwd": "e2e"}]}))
            self.assertEqual(load(root)[0].argv[-1], "provider")
            self.assertIsNotNone(load(root)[0].preparation)
