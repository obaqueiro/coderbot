import unittest
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from check_plan import discover, parse, reported, reported_error, remember
from check_results import parse_output


class CheckPlanTests(unittest.TestCase):
    def test_eslint_counts_every_error_and_keeps_semantic_messages(self):
        output = "/repo/src/a.ts\n  19:38  error  Bad regex  no-control-regex\n\n/repo/src/b.ts\n  2:4  error  Missing cause  preserve-caught-error\n\n✖ 2 problems (2 errors, 0 warnings)"
        result = parse_output(output, 1, "eslint")
        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["failures"]["/repo/src/a.ts:19:38:no-control-regex"], "Bad regex")
        self.assertEqual(parse_output(output.replace("2 errors", "3 errors"), 1, "eslint")["status"], "unknown")
    def test_discovered_python_keeps_real_virtualenv(self):
        with tempfile.TemporaryDirectory() as root:
            repo = Path(root)
            backend = repo / "backend"
            (backend / "tests").mkdir(parents=True)
            subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(backend / ".venv")], check=True)
            plan = {c.id: c for c in discover(repo)}
            python = str(backend / ".venv/bin/python")
            self.assertEqual(plan["unit:backend"].argv[0], python)
            result = subprocess.run([python, "-c", "import sys; print(sys.prefix)"], text=True,
                                    capture_output=True, check=True)
            self.assertEqual(Path(result.stdout.strip()).resolve(), (backend / ".venv").resolve())

    def test_nested_checks_are_declared_by_the_repository(self):
        with tempfile.TemporaryDirectory() as root:
            repo = Path(root)
            (repo / "services/worker/tests").mkdir(parents=True)
            self.assertEqual(discover(repo), [])
            (repo / ".codebot").mkdir()
            plan = {"version": 1, "checks": [{"id": "worker", "argv": [sys.executable,
                    "-m", "unittest", "discover", "-s", "tests"], "cwd": "services/worker"}]}
            (repo / ".codebot/checks.json").write_text(json.dumps(plan))
            self.assertEqual([check.to_dict() for check in discover(repo)],
                             [check.to_dict() for check in parse(plan)])

    def test_missing_test_dependencies_are_infrastructure_not_baseline_failures(self):
        result = parse_output("ERROR: test_api\nModuleNotFoundError: No module named 'fastapi'\nRan 1 test in 0.1s", 1, "unittest")
        self.assertEqual(result["status"], "infrastructure")
        self.assertEqual(result["failures"], {})
    def test_reported_plan_requests_real_execution(self):
        self.assertEqual(reported("QUALITY_GATE: {}"), [])
        plan = reported('summary\nCHECK_PLAN: {"version":1,"checks":[{"id":"unit","argv":["test"],"scope":"focused"}]}')
        self.assertEqual(plan[0].scope, "focused")

    def test_reported_multiline_and_fenced_json(self):
        value = {"version": 1, "checks": [{"id": "unit", "argv": ["test"]}]}
        for prefix in ("CHECK_PLAN: ", "CHECK_PLAN:\n", "CHECK_PLAN:\n```json\n", "CHECK_PLAN:\n```\n"):
            with self.subTest(prefix=prefix):
                output = prefix + json.dumps(value, indent=2) + "\n```\nSummary"
                self.assertEqual(reported(output)[0].id, "unit")
                self.assertIsNone(reported_error(output))

    def test_malformed_agent_plan_is_reported_without_crashing(self):
        for payload in ("", "not JSON", '{"version":1}', '{"version":1,"checks":[null]}',
                        '{"version":1,"checks":[{"id":"bad","argv":[]}]}',
                        '{"version":1,"checks":[{"id":"bad","argv":["test"],"unexpected":true}]}'):
            with self.subTest(payload=payload):
                output = "Exploration complete\nCHECK_PLAN: " + payload
                self.assertEqual(reported(output), [])
                self.assertIsNotNone(reported_error(output))

    def test_invalid_latest_plan_does_not_execute_earlier_plan(self):
        output = 'CHECK_PLAN: {"version":1,"checks":[{"id":"old","argv":["test"]}]}\nCHECK_PLAN: invalid'
        self.assertEqual(reported(output), [])
        self.assertIsNotNone(reported_error(output))
    def test_unknown_output_never_becomes_identifiable_failure(self):
        self.assertEqual(parse_output("connection failed", 1, "unittest")["status"], "unknown")
        result = parse_output("FAIL: test_x (tests.X)\n----------------------------------------------------------------------\nAssertionError: bad\n----------------------------------------------------------------------\nRan 1 test in 0.1s\nFAILED", 1, "unittest")
        self.assertEqual(result["status"], "fail")
        self.assertIn("test_x (tests.X)", result["failures"])
    def test_trusted_external_command_is_valid_without_approval(self):
        checks = parse({"version": 1, "checks": [{"id": "external", "argv": ["/opt/tools/test"],
            "cwd": "/scratch/worktree", "resources": ["port:8111"], "timeout": 30}]})
        self.assertEqual(checks[0].cwd, "/scratch/worktree")

    def test_invalid_or_duplicate_plan_is_rejected(self):
        for value in ({"version": 2, "checks": []},
                      {"version": 1, "checks": [{"id": "x", "argv": []}]},
                      {"version": 1, "checks": [{"id": "x", "argv": ["a"]}] * 2}):
            with self.assertRaises(ValueError):
                parse(value)

    def test_live_timeout_alias_is_normalized_without_changing_the_command(self):
        entry = {"id": "e2e-compartido", "argv": ["bash", "e2e/run.sh", "tests/workspace-zip-otros.spec.ts",
                 "--grep", "ZIP (mixto|solo desconocidos):"], "cwd": ".", "kind": "e2e", "scope": "focused",
                 "reporter": "playwright", "timeout_seconds": 900}
        original = json.loads(json.dumps(entry))
        output = "CHECK_PLAN: " + json.dumps({"version": 1, "checks": [entry]})
        self.assertIsNone(reported_error(output))
        check = reported(output)[0]
        self.assertEqual(check.timeout, 900)
        self.assertEqual(check.argv, entry["argv"])
        self.assertNotIn("timeout_seconds", check.to_dict())
        self.assertEqual(entry, original)

    def test_matching_aliases_are_safe_but_conflicting_or_invalid_values_are_not(self):
        base = {"id": "browser", "argv": ["test"], "timeout": 900, "timeout_seconds": 900.0}
        self.assertEqual(parse({"version": 1, "checks": [base]})[0].timeout, 900)
        for values in ({"timeout": 900, "timeout_seconds": 60}, {"timeout": 1, "timeout_seconds": True},
                       {"timeout_seconds": True}, {"timeout_seconds": -1}, {"timeout_seconds": "900"}):
            with self.subTest(values=values):
                output = "CHECK_PLAN: " + json.dumps({"version": 1, "checks": [{"id": "browser", "argv": ["test"], **values}]})
                self.assertEqual(reported(output), [])
                error = reported_error(output)
                self.assertIn("browser", error)
                self.assertIn("timeout", error)

    def test_errors_identify_the_check_and_expected_field_contract(self):
        for fields, expected in (({"timeout_millis": 900}, "timeout_millis"), ({"kind": "integration"}, "functional"),
                                 ({"argv": "bash e2e/run.sh"}, "argv"), ({"scope": "all"}, "scope"),
                                 ({"cwd": 12}, "cwd")):
            with self.subTest(fields=fields):
                output = "CHECK_PLAN: " + json.dumps({"version": 1, "checks": [{"id": "zip", "argv": ["test"], **fields}]})
                self.assertEqual(reported(output), [])
                error = reported_error(output)
                self.assertIn("zip", error)
                self.assertIn(expected, error)

    def test_shell_string_and_missing_version_are_not_guessed(self):
        for value in ({"checks": [{"name": "zip", "command": "bash e2e/run.sh"}]},
                      {"version": 1, "checks": [{"name": "zip", "command": "bash e2e/run.sh"}]},
                      {"version": 1.0, "checks": []},
                      {"version": True, "checks": []}):
            with self.subTest(value=value):
                output = "CHECK_PLAN: " + json.dumps(value)
                self.assertEqual(reported(output), [])
                self.assertIsNotNone(reported_error(output))

    def test_new_error_episodes_change_recovery_revision_without_erasing_checks(self):
        state = {"reported_check_plan": [{"id": "existing", "argv": ["test"]}]}
        bad = 'CHECK_PLAN: {"version":1,"checks":[{"id":"zip","argv":["test"],"timeout_millis":900}]}'
        good = 'CHECK_PLAN: {"version":1,"checks":[{"id":"zip","argv":["test"],"timeout_seconds":900}]}'
        remember(state, bad)
        first_revision = state["check_plan_repair_revision"]
        state["check_plan_repair_round"] = 2
        remember(state, bad)
        self.assertEqual(state["check_plan_repair_revision"], first_revision)
        self.assertEqual(state["check_plan_repair_round"], 2)
        remember(state, good)
        self.assertNotIn("reported_check_plan_error", state)
        self.assertNotIn("check_plan_repair_round", state)
        self.assertEqual({entry["id"] for entry in state["reported_check_plan"]}, {"existing", "zip"})
        remember(state, bad)
        self.assertGreater(state["check_plan_repair_revision"], first_revision)

    def test_duplicate_error_names_the_id(self):
        output = 'CHECK_PLAN: {"version":1,"checks":[{"id":"zip","argv":["test"]},{"id":"zip","argv":["test"]}]}'
        self.assertEqual(reported(output), [])
        self.assertIn("Duplicate check id zip", reported_error(output))
