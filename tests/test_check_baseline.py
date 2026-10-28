from pathlib import Path
import subprocess
import tempfile
import unittest

from check_baseline import compare, worktree, baseline_result


class BaselineTests(unittest.TestCase):
    def test_random_fixture_paths_match_but_commit_ids_remain_indeterminate(self):
        f = {'status':'fail', 'failures':{'test':'Missing worktree (/tmp/tmpAb12/worktrees/demo)'}}
        b = {'status':'fail', 'failures':{'test':'Missing worktree (/tmp/tmpCd34/worktrees/demo)'}}
        self.assertEqual(compare(f,b)['status'], 'pass')
        f['failures']['test'] = "AssertionError: None != '" + 'a'*40 + "'"
        b['failures']['test'] = "AssertionError: None != '" + 'b'*40 + "'"
        result=compare(f,b)
        self.assertEqual(result['status'],'indeterminate')
        self.assertEqual(result['regressions'],[])
    def test_dependency_comparison_ignores_app_version_not_dependency_versions(self):
        from check_baseline import dependency_snapshot
        import json
        with tempfile.TemporaryDirectory() as root:
            repo = Path(root)
            subprocess.run(['git', 'init', '-q', root], check=True)
            file = repo / 'package-lock.json'
            value = {'version': '1', 'packages': {'': {'version': '1'}, 'node_modules/x': {'version': '2', 'integrity': 'hash'}}}
            file.write_text(json.dumps(value)); before = dependency_snapshot(repo)
            value['version'] = '3'; value['packages']['']['version'] = '3'
            file.write_text(json.dumps(value)); self.assertEqual(before, dependency_snapshot(repo))
            value['packages']['node_modules/x']['version'] = '4'
            file.write_text(json.dumps(value)); self.assertNotEqual(before, dependency_snapshot(repo))
    def test_baseline_links_declared_nested_dependencies_only_in_scratch(self):
        from check_plan import Check
        from execution_store import ExecutionStore
        with tempfile.TemporaryDirectory() as root:
            repo = Path(root) / "repo"
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            (repo / "services/client").mkdir(parents=True)
            (repo / "services/client/package.json").write_text('{"scripts":{}}')
            (repo / ".gitignore").write_text("node_modules/\n")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", "commit", "-qm", "base"], cwd=repo, check=True)
            sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, text=True, capture_output=True, check=True).stdout.strip()
            (repo / "services/client/node_modules").mkdir()
            (repo / "services/client/node_modules/fixture").write_text("installed")
            import sys
            check = Check("dependency", [sys.executable, "-c", "from pathlib import Path; assert Path('node_modules/fixture').read_text() == 'installed'"], cwd="services/client")
            result = baseline_result(check, repo, sha, ExecutionStore(Path(root) / "data/db"))
            self.assertEqual(result["status"], "pass")
            self.assertFalse((repo / "services/client/node_modules").is_symlink())
    def test_lint_paths_are_compared_relative_to_each_worktree(self):
        feature = {"status": "fail", "failures": {"/feature/src/a.ts:2:4:rule": "Bad regex"}}
        baseline = {"status": "fail", "failures": {"<repo>/src/a.ts:2:4:rule": "Bad regex"}}
        self.assertEqual(compare(feature, baseline, roots=("/feature",))["status"], "pass")
    def test_baseline_cached_without_second_worktree_execution(self):
        from check_plan import Check
        from execution_store import ExecutionStore
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as root:
            store = ExecutionStore(Path(root) / "data/db")
            check = Check("unit", ["python", "test"])
            with patch("check_baseline.worktree") as work, \
                 patch("checks.tool_versions", return_value={"python": "fixture"}), \
                 patch("check_baseline.dependency_snapshot", return_value="same"), \
                 patch("check_baseline.execute", return_value={"status": "pass", "failures": {}}) as run:
                work.return_value.__enter__.return_value = Path(root) / "baseline"
                baseline_result(check, root, "a" * 40, store)
                baseline_result(check, root, "a" * 40, store)
            self.assertEqual(run.call_count, 1)
    def test_controller_epoch_invalidates_baseline_cache(self):
        import os
        from check_plan import Check
        from execution_store import ExecutionStore
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as root:
            store = ExecutionStore(Path(root) / "data/db")
            check = Check("unit", ["python", "test"])
            with patch("check_baseline.worktree") as work, \
                 patch("checks.tool_versions", return_value={}), \
                 patch("check_baseline.dependency_snapshot", return_value="same"), \
                 patch("check_baseline.execute", return_value={"status": "pass", "failures": {}}) as run:
                work.return_value.__enter__.return_value = Path(root) / "baseline"
                for version in ("old", "old", "new"):
                    with patch.dict(os.environ, {"CODEBOT_RUNTIME_EPOCH": version}):
                        baseline_result(check, root, "a" * 40, store)
            self.assertEqual(run.call_count, 2)
    def test_semantic_error_difference_is_regression_and_unknown_blocks(self):
        base = {"status": "fail", "failures": {"test": "AssertionError: expected 2"}}
        same = compare(base, base)
        self.assertEqual(same["preexisting"], ["test"])
        changed = {"status": "fail", "failures": {"test": "AssertionError: expected 3"}}
        self.assertEqual(compare(changed, base)["regressions"], ["test"])
        self.assertEqual(compare(base, {"status": "infrastructure"})["status"], "indeterminate")

    def test_differing_baseline_requirements_prepare_separate_interpreter(self):
        from check_plan import Check
        from execution_store import ExecutionStore
        from unittest.mock import patch, Mock
        with tempfile.TemporaryDirectory() as root:
            scratch = Path(root) / "baseline/backend"
            scratch.mkdir(parents=True)
            (scratch / "requirements.txt").write_text("pytz==2026.1\n")
            store = ExecutionStore(Path(root) / "data/db")
            check = Check("unit", ["/feature/backend/.venv/bin/python", "-m", "unittest"], cwd="backend")
            with patch("check_baseline.worktree") as work, \
                 patch("checks.tool_versions", return_value={}), \
                 patch("check_baseline.dependency_snapshot", side_effect=["feature", "base"]), \
                 patch("check_baseline.operations.run", return_value=Mock(returncode=0, stdout="installed", stderr="")) as install, \
                 patch("check_baseline.execute", return_value={"status": "pass", "failures": {}}) as run:
                work.return_value.__enter__.return_value = scratch.parent
                result = baseline_result(check, "/feature", "a" * 40, store)
            self.assertEqual(result["status"], "pass")
            self.assertEqual(install.call_count, 2)
            self.assertEqual(run.call_args.args[0].argv[0], str(scratch / ".venv/bin/python"))
            self.assertIn("requirements.txt", install.call_args.args[0])

    def test_baseline_install_failure_has_diagnostic_not_false_preexisting_result(self):
        from check_plan import Check
        from execution_store import ExecutionStore
        from unittest.mock import patch, Mock
        with tempfile.TemporaryDirectory() as root:
            scratch = Path(root) / "baseline/frontend"
            scratch.mkdir(parents=True)
            (scratch / "package-lock.json").write_text("{}")
            store = ExecutionStore(Path(root) / "data/db")
            with patch("check_baseline.worktree") as work, \
                 patch("checks.tool_versions", return_value={}), \
                 patch("check_baseline.dependency_snapshot", side_effect=["feature", "base"]), \
                 patch("check_baseline.operations.run", return_value=Mock(returncode=1, stdout="", stderr="registry unavailable")), \
                 patch("check_baseline.execute") as run:
                work.return_value.__enter__.return_value = scratch.parent
                result = baseline_result(Check("lint", ["npm", "run", "lint"], cwd="frontend"), root, "a" * 40, store)
            self.assertEqual(result["status"], "infrastructure")
            self.assertIn("registry unavailable", Path(result["report"]).read_text())
            run.assert_not_called()
    def test_worktree_is_pinned_and_removed_after_failure(self):
        with tempfile.TemporaryDirectory() as root:
            repo = Path(root) / "repo"
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            (repo / "app").write_text("baseline")
            subprocess.run(["git", "add", "app"], cwd=repo, check=True)
            subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                "commit", "-qm", "baseline"], cwd=repo, check=True)
            sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, text=True,
                                 capture_output=True, check=True).stdout.strip()
            with self.assertRaisesRegex(RuntimeError, "test failure"):
                with worktree(repo, sha, Path(root) / "data") as path:
                    self.assertEqual((path / "app").read_text(), "baseline")
                    raise RuntimeError("test failure")
            self.assertFalse(path.exists())
