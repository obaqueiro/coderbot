"""Compose validation is read-only; all daemon responses are synthetic."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from workspace import Manifest, Workspace
from workspace_checks import HarnessBlocked, compose_commands, validate_cleanup, validate_docker, validate_resources


class WorkspaceChecksTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / "docker-compose.e2e.yaml").write_text("name: feature-e2e\nservices: {}\n")
        self.context = Workspace(self.root, "workspace", (), Manifest())
        self.script = 'COMPOSE="docker compose --project-name feature-e2e -f $ROOT/docker-compose.e2e.yaml"\n$COMPOSE down --volumes'
        self.config = {"name": "feature-e2e", "services": {"test": {"volumes": [
            {"type": "volume", "source": "test-data", "target": "/data"}]}},
            "volumes": {"test-data": {"name": "feature-e2e_test-data"}},
            "networks": {"default": {"name": "feature-e2e_default"}}}

    def test_explicit_assignment_and_cleanup_commands_are_parsed(self):
        cleanup = 'docker compose -p feature-e2e --file "$root/docker-compose.e2e.yaml" down --volumes'
        self.assertEqual(compose_commands([self.script])[0], ("feature-e2e", ("$ROOT/docker-compose.e2e.yaml",), ()))
        self.assertEqual(compose_commands([cleanup])[0], ("feature-e2e", ("$root/docker-compose.e2e.yaml",), ()))

    def test_env_file_order_and_equal_options_are_supported(self):
        for line in (
            'docker compose --env-file .env -p feature-e2e -f docker-compose.e2e.yaml down --volumes',
            'docker compose --project-name feature-e2e --env-file .env --file docker-compose.e2e.yaml "$@"',
            'docker compose --file=docker-compose.e2e.yaml --env-file=.env --project-name=feature-e2e down -v',
        ):
            with self.subTest(line=line):
                self.assertEqual(compose_commands([line]), [("feature-e2e", ("docker-compose.e2e.yaml",), (".env",))])

    def test_config_validation_preserves_env_file_and_root_fallback(self):
        (self.root / ".env").write_text("TEST=fixture\n")
        script = 'docker compose -p feature-e2e --env-file "${E2E_ENV_FILE:-$ROOT/.env}" -f "$ROOT/docker-compose.e2e.yaml" down --volumes'
        command = Mock(return_value=json.dumps(self.config))
        with patch.dict(os.environ, {}, clear=True):
            validate_cleanup(self.context, [script], command)
        argv = command.call_args.args[1]
        self.assertEqual(argv[argv.index("--env-file") + 1], str(self.root / ".env"))
        override = self.root / "test.env"
        override.touch()
        with patch.dict(os.environ, {"E2E_ENV_FILE": str(override)}, clear=True):
            validate_cleanup(self.context, [script], command)
        argv = command.call_args.args[1]
        self.assertEqual(argv[argv.index("--env-file") + 1], str(override))

    def test_missing_option_values_and_external_env_paths_fail_closed(self):
        with self.assertRaises(HarnessBlocked):
            compose_commands(['docker compose -p feature-e2e -f'])
        with self.assertRaises(HarnessBlocked):
            validate_cleanup(self.context,
                ['docker compose -p feature-e2e -f docker-compose.e2e.yaml --env-file /outside/test.env down --volumes'], Mock())

    def test_only_project_scoped_resources_are_allowed(self):
        validate_resources(self.config, "feature-e2e")
        for section, resource in (("volumes", {"name": "development_data"}),
                                  ("volumes", {"external": True}),
                                  ("volumes", {"driver_opts": {"device": "/dev/shared"}}),
                                  ("networks", {"name": "development"}),
                                  ("networks", {"external": True})):
            with self.subTest(section=section, resource=resource):
                config = {**self.config, section: {"test-data": resource}}
                with self.assertRaises(HarnessBlocked):
                    validate_resources(config, "feature-e2e")

    def test_unnamed_or_development_project_is_rejected(self):
        for command in ('docker compose -f "$ROOT/docker-compose.e2e.yaml" down --volumes',
                        'docker compose -p development -f "$ROOT/docker-compose.e2e.yaml" down --volumes',
                        'docker compose -p "$PROJECT" -f "$ROOT/docker-compose.e2e.yaml" down --volumes'):
            with self.subTest(command=command), self.assertRaises(HarnessBlocked):
                compose_commands([command])

    def test_cleanup_validation_only_calls_compose_config(self):
        command = Mock(return_value=json.dumps(self.config))
        validate_cleanup(self.context, [self.script], command)
        argv = command.call_args.args[1]
        self.assertEqual(argv[-3:], ["config", "--format", "json"])
        self.assertNotIn("down", argv)
        self.assertNotIn("up", argv)
        self.assertIn(str(self.root / "docker-compose.e2e.yaml"), argv)

    def test_local_socket_needs_no_extra_mapping_and_remote_requires_mapping(self):
        command = Mock(side_effect=["default", '"unix:///var/run/docker.sock"'])
        with patch.dict(os.environ, {}, clear=True):
            validate_docker(self.context, command)
        command = Mock(side_effect=["remote", '"ssh://host"'])
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(HarnessBlocked):
            validate_docker(self.context, command)
        remote = Workspace(self.root, "workspace", (), Manifest(docker_json='{"context":"remote","host_root":"/vm/root"}'))
        with patch.dict(os.environ, {"DOCKER_HOST": "ssh://host"}, clear=True):
            validate_docker(remote, Mock(return_value="remote"))

    def test_context_mismatch_and_unavailable_config_are_repairable(self):
        context = Workspace(self.root, "workspace", (), Manifest(docker_json='{"context":"expected"}'))
        with self.assertRaises(HarnessBlocked):
            validate_docker(context, Mock(return_value="wrong"))
        with self.assertRaises(HarnessBlocked):
            validate_cleanup(self.context, [self.script], Mock(side_effect=RuntimeError("config unavailable")))


if __name__ == "__main__":
    unittest.main()
