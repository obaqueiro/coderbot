"""Contract tests for the shared coderbot/OpenSpec bridge skill."""
import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).parent
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
        quick_reference = self.skill.split("## Quick Reference\n", 1)[1].split("\n## ", 1)[0]
        rows = re.findall(r"(?m)^\| ([^|]+) \| ([^|]+) \|$", quick_reference)
        self.phases = [(phase, contract.strip()) for phase, contract in rows if phase != "Phase"]

    def test_manifest_and_skill_identity(self):
        self.assertEqual(self.manifest["name"], "coderbot-openspec")
        self.assertEqual(self.manifest["author"]["name"], "coderbot")
        self.assertTrue(SKILL.is_file())
        self.assertRegex(
            self.skill,
            r"\A---\nname: coderbot-openspec-workflow\ndescription: "
            + re.escape(DESCRIPTION)
            + r"\n---\n",
        )

    def test_description_contains_only_the_headless_workflow_trigger(self):
        description = re.search(r"^description: (.+)$", self.skill, re.MULTILINE).group(1)
        self.assertEqual(description, DESCRIPTION)
        self.assertTrue(description.startswith("Use when"))

    def test_assigns_system_ownership(self):
        for phrase in (
            "OpenSpec owns requirements, design, and tasks",
            "coderbot owns branch, state, email approvals, archive timing, push, PR, and merge",
            "Superpowers owns engineering discipline",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.skill)

    def test_hard_boundaries_use_exact_lifecycle_prohibition(self):
        hard_boundaries = self.skill.split("## Hard Boundaries\n", 1)[1].split("\n## ", 1)[0]
        first_paragraph = hard_boundaries.strip().split("\n\n", 1)[0]
        self.assertEqual(first_paragraph, LIFECYCLE_PROHIBITION)

    def test_skill_rejects_contradictory_agent_lifecycle_permission(self):
        self.assertIsNone(AGENT_LIFECYCLE_PERMISSION.search(self.skill))
        contradictions = (
            "The agent may push after verification.",
            "Agent can create a PR when checks pass.",
            "The agent should merge after approval.",
            "Agent is allowed to choose archive timing.",
            "The agent will finish the branch.",
        )
        for contradiction in contradictions:
            with self.subTest(contradiction=contradiction):
                self.assertIsNotNone(AGENT_LIFECYCLE_PERMISSION.search(contradiction))

    def test_planning_and_checkout_are_concise_positive_ownership(self):
        self.assertIn("Use the checkout coderbot supplied", self.skill)
        self.assertIn("Treat approved OpenSpec artifacts as the only plan", self.skill)
        self.assertNotIn("create a nested worktree", self.skill)
        self.assertNotIn("create duplicate planning artifacts", self.skill)
        self.assertEqual(self.skill.lower().count("worktree"), 0)
        self.assertEqual(self.skill.lower().count("checkout"), 1)

    def test_requires_strict_tdd_for_inherited_production_code(self):
        self.assertIn("Strict TDD applies to inherited production code", self.skill)
        self.assertIn("delete or revert it and restart test-first", self.skill)
        self.assertIn("previous agent", self.skill)

    def test_phase_contract_has_exact_order(self):
        expected = [
            "EXPLORING",
            "PROPOSING",
            "IMPLEMENTING",
            "VERIFYING",
            "INTERNAL_REVIEW",
            "E2E repair",
            "ARCHIVING / OPEN_PR",
        ]
        self.assertEqual([phase for phase, _ in self.phases], expected)

    def test_lifecycle_owner_mapping_is_non_permissive(self):
        contracts = dict(self.phases)
        lifecycle = contracts["ARCHIVING / OPEN_PR"]
        self.assertTrue(lifecycle.startswith("Coderbot alone"))
        for action in ("archive", "push", "creates the PR", "merge"):
            with self.subTest(action=action):
                self.assertIn(action, lifecycle)
        for phase, contract in self.phases[:-1]:
            with self.subTest(phase=phase):
                self.assertNotRegex(contract, r"\b(archives?|push(?:es)?|creates? the PR|merges?)\b")

    def test_headless_input_contract_forbids_interactive_questions(self):
        self.assertIn("truly material unresolved decision", self.skill)
        self.assertIn("return `NEED_USER_INPUT`", self.skill)
        self.assertIn("Do not ask an interactive question", self.skill)

    def test_verification_requires_fresh_evidence_openspec_validation_and_review(self):
        contracts = dict(self.phases)
        self.assertIn("fresh full relevant verification", contracts["VERIFYING"])
        self.assertIn("strictly validates OpenSpec", contracts["VERIFYING"])
        self.assertIn("fresh internal review", contracts["INTERNAL_REVIEW"])
        self.assertIn("Prior test evidence", contracts["INTERNAL_REVIEW"])
        self.assertIn("future external review", contracts["INTERNAL_REVIEW"])
        self.assertIn("not substitutes", contracts["INTERNAL_REVIEW"])

    def test_tracked_e2e_repairs_repeat_verification_and_internal_review(self):
        contract = dict(self.phases)["E2E repair"]
        self.assertIn("tracked code", contract)
        self.assertLess(contract.index("VERIFYING"), contract.index("INTERNAL_REVIEW"))
        self.assertIn("before coderbot retries E2E", contract)

    def test_implementing_does_not_perform_verifying_phase_work(self):
        implementing = dict(self.phases)["IMPLEMENTING"]
        self.assertNotRegex(implementing, r"\b(full verification|verify|verification)\b")
        example = self.skill.split("## Example\n", 1)[1].split("\n## ", 1)[0]
        self.assertNotIn("full verification", example)
        self.assertIn("hand control back to coderbot", example)

    def test_contains_required_reference_and_discipline_sections(self):
        for heading in (
            "## Quick Reference",
            "## Example",
            "## Rationalization Counters",
            "## Red Flags",
            "## Common Mistakes",
        ):
            with self.subTest(heading=heading):
                self.assertIn(heading, self.skill)

        self.assertIn("| Excuse | Reality |", self.skill)
        examples = re.findall(r"^## Example$", self.skill, re.MULTILINE)
        self.assertEqual(len(examples), 1)


if __name__ == "__main__":
    unittest.main()
