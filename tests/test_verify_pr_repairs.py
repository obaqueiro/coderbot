"""Regression coverage for content-bound checks and controller-owned completion."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import tests
import main
import config
from check_plan import Check
from check_baseline import dependency_inputs, dependency_snapshot
from execution_identity import snapshot
from execution_store import ExecutionStore


class VerifyPrRepairTests(unittest.TestCase):
    def test_root_dot_snapshot_covers_source_changes(self):
        with tempfile.TemporaryDirectory() as root:
            subprocess.run(['git', 'init', '-q', root], check=True)
            file = Path(root) / 'source.py'
            file.write_text('before')
            before = snapshot(root, ['.'])
            file.write_text('after')
            self.assertNotEqual(before, snapshot(root, ['.']))

    def test_backend_dependency_scope_ignores_e2e_and_legacy_changes(self):
        with tempfile.TemporaryDirectory() as root:
            repo = Path(root)
            subprocess.run(['git', 'init', '-q', root], check=True)
            for area in ('backend', 'e2e', 'legacy/backend'):
                (repo / area).mkdir(parents=True)
                (repo / area / 'requirements.txt').write_text('fixture==1')
            inputs = dependency_inputs(Check('unit', ['python', 'test'], cwd='backend'), repo)
            before = dependency_snapshot(repo, inputs)
            (repo / 'e2e/requirements.txt').write_text('fixture==2')
            (repo / 'legacy/backend/requirements.txt').write_text('fixture==3')
            self.assertEqual(before, dependency_snapshot(repo, inputs))
            (repo / 'backend/requirements.txt').write_text('fixture==2')
            self.assertNotEqual(before, dependency_snapshot(repo, inputs))

    def test_recovery_and_review_receive_current_scope(self):
        import recovery
        import handoff_context
        state = {'slug': 'feature', 'verification_guidance': 'Exclude legacy/** and preserve it'}
        self.assertIn(state['verification_guidance'], main._internal_review_prompt(state))
        self.assertIn(state['verification_guidance'], recovery.prompt({'data': {'reason': 'dirty'}}, [], state))
        self.assertIn(state['verification_guidance'], handoff_context.render(state, 'INTERNAL_REVIEW'))

    def test_dependency_install_identity_ignores_scripts_but_not_packages(self):
        with tempfile.TemporaryDirectory() as root:
            repo = Path(root)
            subprocess.run(['git', 'init', '-q', root], check=True)
            file = repo / 'package.json'
            value = {'scripts': {'test': 'node --test'}, 'dependencies': {'fixture': '1'}}
            file.write_text(json.dumps(value))
            before = dependency_snapshot(repo)
            value['scripts']['test'] += ' --test-timeout=600000'
            file.write_text(json.dumps(value))
            self.assertEqual(before, dependency_snapshot(repo))
            value['dependencies']['fixture'] = '2'
            file.write_text(json.dumps(value))
            self.assertNotEqual(before, dependency_snapshot(repo))

    def test_review_rejects_controller_failure_even_with_pass_contract(self):
        state = {'state': 'INTERNAL_REVIEW', 'slug': 'feature', 'item': 'task'}
        output = ('CHECK_PLAN: {"version":1,"checks":[{"id":"focused","argv":["test"],"scope":"focused"}]}\n'
                  'INTERNAL_REVIEW: {"status":"pass","critical":0,"important":0,"tests":["focused: pass"]}')
        result = Mock(session_id='session', output=output, question=None)
        with patch.object(main, '_scope_drift', return_value=False), \
             patch.object(main, '_transcript_append'), patch.object(main.phase_checkpoint, 'store') as store, \
             patch.object(main.checks, 'execute_plan', return_value=[{'status': 'fail'}]), \
             patch.object(main, '_begin_check_repair') as reject:
            main._complete_internal_review(state, result)
        reject.assert_called_once()
        self.assertNotIn('internal_review_report', state)
        self.assertEqual(state['state'], 'INTERNAL_REVIEW')

    def test_final_repair_invalidates_old_gates_and_reenters_verify(self):
        state = {'state': 'E2E', 'session_id': 'session', 'slug': 'feature', 'item': 'task', 'quality_report': {'status': 'pass'},
                 'internal_review_report': {'status': 'pass'}, 'quality_controller_validated': True}
        report = {'status': 'fail', 'checks': [{'check': 'unit', 'gate': {'regressions': ['t']}}]}
        with patch.object(config, 'DETERMINISTIC_CHECKS', True), \
             patch.object(main, '_scope_drift', return_value=False), patch.object(main, 'save_state'), \
             patch.object(main.final_checks, 'run', return_value=report), \
             patch.object(main, 'email'), patch.object(main, 'trail'), \
             patch.object(main, 'content_snapshot', side_effect=['before', 'after']), \
             patch.object(main, '_tracked_snapshot', return_value=('head', '')), \
             patch.object(main.phase_checkpoint, 'store'), patch.object(main.checks, 'invalidate_environment'), \
             patch.object(main.agent_runner, 'resume'), patch.object(main, 'handle_result', return_value=False):
            main.do_e2e(state)
            self.assertEqual(state['state'], 'REPAIR_CHECKS')
            main._complete_check_repair(state, Mock(output='fixed'))
        self.assertIn(state['state'], ('VERIFYING', 'E2E'))
        self.assertNotIn('quality_report', state)
        self.assertNotIn('internal_review_report', state)

    def test_final_checkbox_commit_is_restart_safe_and_scoped(self):
        with tempfile.TemporaryDirectory() as root:
            repo = Path(root) / 'repo'
            repo.mkdir()
            subprocess.run(['git', 'init', '-q', str(repo)], check=True)
            for key, value in (('user.name', 'Fixture'), ('user.email', 'fixture@local.invalid')):
                subprocess.run(['git', 'config', key, value], cwd=repo, check=True)
            task = repo / 'openspec/changes/feature/tasks.md'
            task.parent.mkdir(parents=True)
            task.write_text('- [x] Implement\n- [ ] Final [codebot:final-checks]\n')
            subprocess.run(['git', 'add', '.'], cwd=repo, check=True)
            subprocess.run(['git', 'commit', '-qm', 'base'], cwd=repo, check=True)
            store = ExecutionStore(Path(root) / 'data/db')
            state = {'state': 'E2E', 'item': 'task', 'slug': 'feature', 'branch': 'fixture',
                     'final_check_report': {'status': 'pass', 'snapshot': 'tested'}}
            with patch.object(config, 'REPO_PATH', repo), patch.object(main.phase_checkpoint, 'store', return_value=store):
                # Simulate interruption after the controller staged its exact delta.
                task.write_text(task.read_text().replace('- [ ] Final', '- [x] Final'))
                subprocess.run(['git', 'add', str(task)], cwd=repo, check=True)
                main._commit_final_check_tasks(state)
                head = main.git('rev-parse', 'HEAD')
                main._commit_final_check_tasks(state)
                self.assertEqual(head, main.git('rev-parse', 'HEAD'))
                self.assertEqual(main.git('status', '--porcelain'), '')
                self.assertEqual(main.git('diff-tree', '--no-commit-id', '--name-only', '-r', head),
                                 'openspec/changes/feature/tasks.md')
                rows = store.list('provenance', store.task_identity(state, repo))
                self.assertTrue(any(r['data'].get('kind') == 'controller-final-task-completion' for r in rows))

    def test_explicit_active_manifest_ignores_legacy_in_discovery_and_snapshots(self):
        from check_plan import discover
        with tempfile.TemporaryDirectory() as root:
            repo = Path(root)
            subprocess.run(['git', 'init', '-q', root], check=True)
            (repo / '.codebot').mkdir()
            (repo / '.codebot/checks.json').write_text(json.dumps({'version': 1, 'checks': [
                {'id': 'active', 'argv': ['test'], 'inputs': ['backend'],
                 'dependency_inputs': ['backend/requirements.txt']}]}))
            (repo / 'backend').mkdir()
            (repo / 'backend/source.py').write_text('active')
            (repo / 'legacy/backend/tests').mkdir(parents=True)
            legacy = repo / 'legacy/backend/tests/test_broken.py'
            legacy.write_text('raise RuntimeError("historical")')
            plan = discover(repo)
            self.assertEqual([c.id for c in plan], ['active'])
            before = snapshot(repo, plan[0].inputs)
            legacy.write_text('different historical failure')
            self.assertEqual(before, snapshot(repo, plan[0].inputs))

    def test_final_completion_preserves_unrelated_task_edits(self):
        with tempfile.TemporaryDirectory() as root:
            repo = Path(root)
            subprocess.run(['git', 'init', '-q', root], check=True)
            task = repo / 'openspec/changes/feature/tasks.md'
            task.parent.mkdir(parents=True)
            original = '- [ ] Final [codebot:final-checks]\n'
            task.write_text(original)
            subprocess.run(['git', 'add', '.'], cwd=repo, check=True)
            subprocess.run(['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@local.invalid',
                            'commit', '-qm', 'base'], cwd=repo, check=True)
            task.write_text(original + 'Unrelated user notes\n')
            before = task.read_text()
            with patch.object(config, 'REPO_PATH', repo), self.assertRaisesRegex(RuntimeError, 'unrelated tasks.md'):
                main._commit_final_check_tasks({'slug': 'feature', 'final_check_report': {'status': 'pass'}})
            self.assertEqual(before, task.read_text())
