"""Contract tests for the shared coderbot/OpenSpec bridge skill."""
import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "agent-plugin/.claude-plugin/plugin.json"
SKILL = ROOT / "agent-plugin/skills/coderbot-openspec-workflow/SKILL.md"
DESCRIPTION = "Use when a headless coderbot agent is working on an OpenSpec-managed change"
LIFECYCLE_PROHIBITION = (
    "The agent must never push, create a PR, merge, choose archive timing, "
    "finish the branch, or choose a merge strategy."
)
AGENT_LIFECYCLE_PERMISSION = re.compile(
    r"(?i)\b(?:the )?agent\s+(?:may|can|should|will|is allowed to)\s+"
    r"(?:archive|push|create (?:a|the) PR|merge|choose (?:archive timing|a merge strategy)|finish the branch)"
)


class BridgeSkillTests(unittest.TestCase):
    def setUp(self):
        self.manifest = json.loads(MANIFEST.read_text())
        self.skill = SKILL.read_text()

    def test_manifest_and_skill_identity(self):
        self.assertEqual(self.manifest["name"], "coderbot-openspec")
        self.assertEqual(self.manifest["author"]["name"], "coderbot")
        self.assertRegex(self.skill, r"\A---\nname: coderbot-openspec-workflow\ndescription: "
                         + re.escape(DESCRIPTION) + r"\n---\n")

    def test_adapter_is_short_and_binds_instead_of_overriding(self):
        self.assertLess(len(self.skill), 4200)
        self.assertNotIn("override", self.skill.lower())
        for skill in ("brainstorming", "openspec-propose", "writing-plans", "executing-plans",
                      "openspec-apply-change", "test-driven-development", "systematic-debugging",
                      "verification-before-completion", "requesting-code-review", "receiving-code-review",
                      "using-git-worktrees", "finishing-a-development-branch"):
            with self.subTest(skill=skill):
                self.assertIn(skill, self.skill)

    def test_every_interactive_step_maps_to_a_result_status_or_field(self):
        for binding in ("`needs_input`", "`blocked`", "`classification`", "`checks`", "`review`", "`threads`",
                        "Inline Execution", "openspec/changes/<change>/design.md", "openspec/changes/<change>/plan.md"):
            with self.subTest(binding=binding):
                self.assertIn(binding, self.skill)

    def test_assigns_system_ownership(self):
        for phrase in ("OpenSpec owns requirements, design, and tasks", "Superpowers owns engineering discipline",
                       "coderbot owns branch, state, email approvals, archive timing, push, PR, and merge"):
            self.assertIn(phrase, self.skill)

    def test_hard_boundaries_keep_the_exact_lifecycle_prohibition(self):
        hard = self.skill.split("## Hard Boundaries\n", 1)[1].strip().split("\n\n", 1)[0]
        self.assertEqual(hard, LIFECYCLE_PROHIBITION)
        self.assertIsNone(AGENT_LIFECYCLE_PERMISSION.search(self.skill))
        for contradiction in ("The agent may push after verification.", "Agent can create a PR when checks pass."):
            self.assertIsNotNone(AGENT_LIFECYCLE_PERMISSION.search(contradiction))


if __name__ == "__main__":
    unittest.main()
