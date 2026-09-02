"""Source-contract tests for the interactive setup script."""
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent


class SetupScriptTests(unittest.TestCase):
    def test_round_limits_are_displayed_validated_written_and_summarized(self):
        source = (ROOT / "setup.sh").read_text()
        current_settings = source[source.index("print_current_settings()"):
                                  source.index("prepare_env_write()")]
        prompts = source[source.index("CODEBOT_LOG_LEVEL=$(prompt_var"):
                         source.index("CLAUDE_API_KEY=\"\"")]
        env_write_start = source.index('cat >"$ENV_FILE" <<EOF')
        env_write = source[env_write_start:
                           source.index("\nEOF", env_write_start)]
        summary = source[source.index('echo "Wrote $ENV_FILE with:"'):
                         source.index("fi", source.index('echo "Wrote $ENV_FILE with:"'))]

        for variable in (
                "CODEBOT_QUALITY_GATE_MAX_ROUNDS",
                "CODEBOT_ARCHIVE_MAX_ROUNDS",
        ):
            with self.subTest(variable=variable):
                self.assertIn(variable, current_settings)
                self.assertRegex(
                    prompts,
                    rf'{variable}=\$\(prompt_var "{variable}"[\s\S]*?"3"\)'
                    rf'[\s\S]*?"\${variable}" =~ \^\[1-9\]\[0-9\]\*\$',
                )
                self.assertIn(f"{variable}=${variable}", env_write)
                self.assertIn(variable, summary)


if __name__ == "__main__":
    unittest.main()
