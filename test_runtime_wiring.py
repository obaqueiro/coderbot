"""Startup behavior tests for the managed agent runtime."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

with patch.dict("sys.modules", {
    "gdoc_client": MagicMock(),
    "gmail_client": MagicMock(),
}):
    import main


class RuntimeValidationTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        self.superpowers = root / "superpowers"
        self.bridge = root / "bridge"
        for path in (
            self.superpowers / ".claude-plugin/plugin.json",
            self.superpowers / "skills/using-superpowers/SKILL.md",
            self.superpowers / ".opencode/plugins/superpowers.js",
            self.bridge / ".claude-plugin/plugin.json",
            self.bridge / "skills/coderbot-openspec-workflow/SKILL.md",
            self.bridge / "package.json",
            self.bridge / ".opencode/plugins/coderbot-openspec.js",
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fixture")
        self.path_patches = (
            patch.object(main.config, "SUPERPOWERS_PLUGIN_DIR", self.superpowers, create=True),
            patch.object(main.config, "BRIDGE_PLUGIN_DIR", self.bridge, create=True),
        )
        for path_patch in self.path_patches:
            path_patch.start()

    def tearDown(self):
        for path_patch in reversed(self.path_patches):
            path_patch.stop()
        self.temporary_directory.cleanup()

    def validate(self):
        self.assertTrue(hasattr(main, "validate_managed_runtime"),
                        "managed runtime validator is missing")
        main.validate_managed_runtime()

    def test_fixture_runtime_is_valid_for_each_agent(self):
        for agent in ("claude", "opencode"):
            with self.subTest(agent=agent), patch.object(main.config, "AGENT", agent):
                self.validate()

    def test_each_missing_claude_component_fails_clearly(self):
        components = {
            "Superpowers Claude manifest": self.superpowers / ".claude-plugin/plugin.json",
            "Superpowers skill tree": self.superpowers / "skills/using-superpowers/SKILL.md",
            "bridge manifest": self.bridge / ".claude-plugin/plugin.json",
            "bridge skill": self.bridge / "skills/coderbot-openspec-workflow/SKILL.md",
        }
        with patch.object(main.config, "AGENT", "claude"):
            for label, path in components.items():
                with self.subTest(component=label):
                    contents = path.read_text()
                    path.unlink()
                    try:
                        with self.assertRaisesRegex(SystemExit, label):
                            self.validate()
                    finally:
                        path.write_text(contents)

    def test_each_missing_opencode_component_fails_clearly(self):
        components = {
            "Superpowers OpenCode entrypoint": self.superpowers / ".opencode/plugins/superpowers.js",
            "Superpowers skill tree": self.superpowers / "skills/using-superpowers/SKILL.md",
            "bridge manifest": self.bridge / ".claude-plugin/plugin.json",
            "bridge skill": self.bridge / "skills/coderbot-openspec-workflow/SKILL.md",
            "bridge OpenCode package": self.bridge / "package.json",
            "bridge OpenCode entrypoint": self.bridge / ".opencode/plugins/coderbot-openspec.js",
        }
        with patch.object(main.config, "AGENT", "opencode"):
            for label, path in components.items():
                with self.subTest(component=label):
                    contents = path.read_text()
                    path.unlink()
                    try:
                        with self.assertRaisesRegex(SystemExit, label):
                            self.validate()
                    finally:
                        path.write_text(contents)

    def test_main_validates_runtime_before_backlog_selection(self):
        backlog_selection = MagicMock(side_effect=SystemExit("backlog selection reached"))
        with patch.object(main.config, "AGENT", "claude"), \
             patch.object(main.config, "USER_EMAIL", "user@example.test"), \
             patch.object(main.config, "DOC_ID", "doc"), \
             patch.object(main.config, "REPO_PATH", Path(self.temporary_directory.name)), \
             patch.object(Path, "exists", return_value=True), \
             patch.object(main, "validate_managed_runtime",
                          side_effect=SystemExit("runtime unavailable"), create=True), \
             patch.object(main, "load_state", return_value={"state": "IDLE"}), \
             patch.object(main, "check_abort", return_value=False), \
             patch.dict(main.PHASES, {"IDLE": backlog_selection}):
            with self.assertRaisesRegex(SystemExit, "runtime unavailable"):
                main.main()
        backlog_selection.assert_not_called()


if __name__ == "__main__":
    unittest.main()
