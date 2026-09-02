"""Behavioral contract tests for rendered lifecycle prompts."""
import sys
import unittest
from unittest.mock import Mock, patch

import prompts

with patch.dict(sys.modules, {"gdoc_client": Mock(), "gmail_client": Mock()}):
    import main


class PromptContractTests(unittest.TestCase):
    def assert_no_integration_authority(self, rendered):
        for contradictory_directive in (
            "Push the branch",
            "Create a pull request",
            "Merge the branch",
            "Archive the OpenSpec change",
        ):
            with self.subTest(contradictory_directive=contradictory_directive):
                self.assertNotIn(contradictory_directive, rendered)

    def test_explore_is_analysis_only_and_routes_material_decisions_to_user(self):
        rendered = prompts.render(
            prompts.EXPLORE,
            project="example",
            branch="codebot-change",
            item="support a second API version",
        )

        self.assertIn(
            "This phase is exploration only. Do not implement the change or modify project code.",
            rendered,
        )
        self.assertIn(
            "Invoke `coderbot-openspec-workflow`, `openspec-explore`, and `brainstorming`.",
            rendered,
        )
        self.assertIn(
            "For any material decision affecting scope, observable behavior, compatibility, "
            "or acceptance criteria, return `NEED_USER_INPUT`; make minor decisions autonomously.",
            rendered.replace("\n", " "),
        )
        self.assertNotIn("implementing this improvement", rendered)
        self.assertNotIn("Begin implementation", rendered)
        self.assert_no_integration_authority(rendered)

    def test_propose_creates_only_reviewable_openspec_artifacts(self):
        e2e_note = main._e2e_note({"has_e2e_harness": True, "e2e_kind": "playwright"})
        rendered = prompts.render(
            prompts.PROPOSE,
            slug="api-version",
            e2e_note=e2e_note,
        )

        self.assertIn(
            "Invoke `coderbot-openspec-workflow` and `openspec-propose` to formalize "
            "the exploration as change api-version.",
            rendered.replace("\n", " "),
        )
        self.assertIn(
            "Create only OpenSpec artifacts: proposal.md, design.md, specs, and tasks.md.",
            rendered.replace("\n", " "),
        )
        self.assertIn(
            "Do not implement, commit, push, create a PR, merge, or archive; stop for "
            "coderbot's proposal approval.",
            rendered.replace("\n", " "),
        )
        self.assertIn(e2e_note, rendered)
        self.assert_no_integration_authority(rendered)

    def test_propose_absent_harness_does_not_require_e2e(self):
        e2e_note = main._e2e_note({"has_e2e_harness": False})
        rendered = prompts.render(prompts.PROPOSE, slug="api-version", e2e_note=e2e_note)

        self.assertIn(e2e_note, rendered)
        self.assertNotIn("MUST include comprehensive", rendered)
        self.assertNotIn("automated evidence step", rendered)
        self.assertNotIn("E2E_SPEC:", rendered)

    def test_implement_uses_tdd_and_debugging_without_integration_authority(self):
        state = {"has_e2e_harness": True, "e2e_kind": "newman"}
        e2e_note = main._e2e_note(state)
        e2e_report_note = main._e2e_report_note(state)
        rendered = prompts.render(
            prompts.IMPLEMENT,
            slug="api-version",
            branch="codebot-api-version",
            e2e_note=e2e_note,
            e2e_report_note=e2e_report_note,
        )

        self.assertIn(
            "Invoke `coderbot-openspec-workflow`, `openspec-apply-change`, and "
            "`test-driven-development` to implement change api-version.",
            rendered.replace("\n", " "),
        )
        self.assertIn(
            "If a test or technical check fails, invoke `systematic-debugging` before fixing it.",
            rendered,
        )
        self.assertIn(
            "Coderbot retains integration authority: do not push, create a PR, merge, or archive.",
            rendered,
        )
        self.assertIn("Do not commit any evidence file", rendered)
        self.assertIn(e2e_note, rendered)
        self.assertIn(e2e_report_note, rendered)
        self.assert_no_integration_authority(rendered)

    def test_implement_absent_harness_does_not_require_e2e_or_evidence_report(self):
        state = {"has_e2e_harness": False}
        e2e_note = main._e2e_note(state)
        e2e_report_note = main._e2e_report_note(state)
        rendered = prompts.render(
            prompts.IMPLEMENT,
            slug="api-version",
            branch="codebot-api-version",
            e2e_note=e2e_note,
            e2e_report_note=e2e_report_note,
        )

        self.assertIn(e2e_note, rendered)
        self.assertIn(e2e_report_note, rendered)
        self.assertNotIn("MUST include comprehensive", rendered)
        self.assertNotIn("automated evidence step", rendered)
        self.assertNotIn("E2E_SPEC:", rendered)

    def test_verify_requires_fresh_complete_evidence_and_exact_final_contract(self):
        rendered = prompts.render(prompts.VERIFY, slug="api-version")
        contract = (
            'QUALITY_GATE: {"status":"pass","commands":["<command: result>"],'
            '"openspec":"pass","tasks":"N/N"}'
        )

        self.assertIn(
            "Run fresh, complete relevant verification commands in this phase; do not reuse "
            "prior evidence.",
            rendered.replace("\n", " "),
        )
        self.assertIn(
            "Strictly validate the active OpenSpec change and confirm all OpenSpec apply tasks "
            "are complete.",
            rendered.replace("\n", " "),
        )
        self.assertNotIn("Prior evidence is acceptable", rendered)
        self.assertNotIn("Skip commands already run", rendered)
        self.assert_no_integration_authority(rendered)
        self.assertEqual(rendered.count(contract), 1)
        self.assertEqual(rendered.strip().splitlines()[-1], contract)

    def test_internal_review_requires_fresh_review_fixes_and_exact_final_contract(self):
        rendered = prompts.render(prompts.INTERNAL_REVIEW, slug="api-version")
        contract = (
            'INTERNAL_REVIEW: {"status":"pass","critical":0,"important":0,'
            '"tests":["<command: result>"]}'
        )

        self.assertIn(
            "Internal review is mandatory. Invoke `requesting-code-review` with a fresh "
            "reviewer subagent.",
            rendered.replace("\n", " "),
        )
        self.assertIn(
            "Prior test evidence and future external review are not substitutes.", rendered
        )
        self.assertIn(
            "Fix every Critical or Important finding, rerun tests covering the fixes, and "
            "obtain a clean re-review.",
            rendered.replace("\n", " "),
        )
        self.assertIn("Commit all review fixes before emitting the pass contract.", rendered)
        self.assertNotIn("Review is optional", rendered)
        self.assertNotIn("if time permits", rendered)
        self.assert_no_integration_authority(rendered)
        self.assertEqual(rendered.count(contract), 1)
        self.assertEqual(rendered.strip().splitlines()[-1], contract)

    def test_archive_repair_is_limited_to_openspec_planning_files(self):
        rendered = prompts.render(
            prompts.FIX_ARCHIVE,
            error="validation failed",
            guidance="repair the delta",
        )

        self.assertIn("validation failed", rendered)
        self.assertIn("repair the delta", rendered)
        self.assertIn("OpenSpec planning and spec files only", rendered)
        self.assertIn("commit", rendered.lower())
        for prohibition in (
            "do not modify project code", "do not run `openspec archive`",
            "do not push", "do not create a pr",
        ):
            self.assertIn(prohibition, rendered.lower())

    def test_e2e_repair_diagnoses_before_minimal_test_backed_fix(self):
        rendered = prompts.render(
            prompts.FIX_E2E,
            output="AssertionError: expected ${status}, got {'status': 'failed'}",
        )

        self.assertIn(
            "Invoke `coderbot-openspec-workflow` and `systematic-debugging`. Establish the "
            "root cause before any edit",
            rendered.replace("\n", " "),
        )
        self.assertIn(
            "when applicable, write and witness a regression test fail, then make the minimal "
            "fix and run focused tests to GREEN.",
            rendered.replace("\n", " "),
        )
        self.assertIn(
            "Do not create a PR. Do not archive the OpenSpec change, and do not claim the "
            "task or branch is complete.",
            rendered.replace("\n", " "),
        )
        self.assertNotIn("The task is ready for PR", rendered)
        self.assertNotIn("Archive after tests pass", rendered)
        self.assertIn("AssertionError: expected ${status}, got {'status': 'failed'}", rendered)


if __name__ == "__main__":
    unittest.main()
