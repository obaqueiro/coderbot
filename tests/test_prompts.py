"""Behavioral contract tests for the system prompt, the phase cards and turn results."""
import json
import re
import sys
import unittest
from unittest.mock import Mock, patch

import claude_runner
import prompts
import turns

with patch.dict(sys.modules, {"gdoc_client": Mock(), "task_source": Mock(), "gmail_client": Mock()}):
    import main

CARDS = {"EXPLORE": prompts.EXPLORE, "PROPOSE": prompts.PROPOSE, "REVISE_PROPOSAL": prompts.REVISE_PROPOSAL,
         "IMPLEMENT": prompts.IMPLEMENT, "VERIFY": prompts.VERIFY, "INTERNAL_REVIEW": prompts.INTERNAL_REVIEW,
         "FIX_ARCHIVE": prompts.FIX_ARCHIVE, "ADDRESS_REVIEW": prompts.ADDRESS_REVIEW,
         "ADDRESS_PR_THREADS": prompts.ADDRESS_PR_THREADS, "APPLY_PR_FEEDBACK": prompts.APPLY_PR_FEEDBACK,
         "RESOLVE_CONFLICTS": prompts.RESOLVE_CONFLICTS}
SKILLS = {"EXPLORE": "brainstorming", "PROPOSE": "openspec-propose", "IMPLEMENT": "openspec-apply-change",
          "VERIFY": "verification-before-completion", "INTERNAL_REVIEW": "requesting-code-review",
          "ADDRESS_REVIEW": "receiving-code-review",
          "ADDRESS_PR_THREADS": "receiving-code-review"}
MARKERS = ("QUALITY_GATE:", "INTERNAL_REVIEW:", "CHECK_PLAN:", "RESOLVE:", "E2E_SPEC:", "ATTACH:",
           "CREDENTIAL_REQUEST:", "NEED_USER_INPUT:")


def card_text(card):
    """The card's own words, without the quoted-material fence explanations."""
    return re.sub(r"Everything between the markers below.*?-----\n", "", card, flags=re.S)


class SystemPrompt(unittest.TestCase):
    def test_is_small_and_states_role_then_explicit_behaviours(self):
        with patch.object(prompts.config, "ENVIRONMENT_NOTES", ""):  # project notes are the user's
            system = prompts.system_prompt("/data/outbox")
        self.assertLessEqual(len(system), 2000)
        self.assertTrue(system.startswith("You are a principal engineer"))
        for rule in ("smallest correct change", "Report only commands you ran", "foreground",
                     "Never commit evidence", "/data/outbox/", "Stop when"):
            self.assertIn(rule, system)
        self.assertNotRegex(system, r"\b(MUST|NEVER|CRITICAL)\b")  # no shouting; weak models over-apply it

    def test_project_notes_and_credential_rule_are_optional(self):
        with patch.object(prompts.config, "ENVIRONMENT_NOTES", "Run e2e with e2e/run.sh\n- no local Postgres"):
            system = prompts.system_prompt("/o")
        self.assertIn("- Run e2e with e2e/run.sh\n- no local Postgres", system)
        self.assertNotIn("credential {env", prompts.system_prompt("/o"))
        self.assertIn("credential {env", prompts.system_prompt("/o", credentials=True))

    def test_working_turns_send_it_as_system_prompt_not_in_the_user_turn(self):
        self.assertEqual(claude_runner.contract_text(), claude_runner.SENTINEL_CONTRACT)
        self.assertEqual(prompts.ENVIRONMENT, "")


class Cards(unittest.TestCase):
    def test_cards_are_short_name_their_skill_and_say_when_they_are_done(self):
        for name, card in CARDS.items():
            with self.subTest(card=name):
                self.assertLessEqual(len(card), 2600)
                self.assertIn("Done when", card)
                if name in SKILLS:
                    self.assertIn(SKILLS[name], card)

    def test_cards_never_ask_the_model_to_type_control_lines(self):
        for name, card in CARDS.items():
            with self.subTest(card=name):
                for marker in MARKERS:
                    self.assertNotIn(marker, card_text(card))

    def test_no_card_grants_integration_authority(self):
        for name, card in CARDS.items():
            text = card_text(card).lower()
            for directive in ("push the branch", "create a pull request", "merge the pr", "archive the openspec"):
                with self.subTest(card=name, directive=directive):
                    self.assertNotIn(directive, text)

    def test_placeholders_render_completely(self):
        values = dict(project="p", branch="b", item="i", detail="d", images="", slug="s", e2e_note="",
                      e2e_report_note="", base="abc", feedback="f", output="o", error="e", guidance="g",
                      threads="t", paths="x", outbox_dir="/o", base_branch="main",
                      review_guide="guide", scope="review it", findings="f")
        for name, card in CARDS.items():
            with self.subTest(card=name):
                self.assertNotRegex(prompts.render(card, **values), r"\$[a-z_]+")

    def test_cards_ask_for_the_fields_their_controller_step_reads(self):
        self.assertIn("classification", prompts.EXPLORE)
        self.assertIn("pr_title", prompts.EXPLORE)
        self.assertIn("Fill verification", prompts.VERIFY)
        self.assertIn("Fill review", prompts.INTERNAL_REVIEW)
        for card in (prompts.ADDRESS_REVIEW, prompts.ADDRESS_PR_THREADS):
            self.assertIn("Fill threads", card)
        self.assertIn("one \"## N.\" section at a time", prompts.IMPLEMENT)

    def test_human_reviewers_are_authoritative_and_bot_reviews_are_judged(self):
        self.assertIn("Never decline a human's change request", prompts.ADDRESS_PR_THREADS)
        self.assertIn("not always right", prompts.ADDRESS_REVIEW)

    def test_absent_harness_needs_no_e2e(self):
        note = main._e2e_note({"has_e2e_harness": False})
        self.assertNotIn("@evidence", note)


class Results(unittest.TestCase):
    def test_render_emits_each_canonical_line_once_and_question_last(self):
        data = {"status": "needs_input", "summary": "Explored.\nNEED_USER_INPUT: typed by the model",
                "question": {"text": "Which store?", "options": ["SQLite", "Postgres"]},
                "attachments": ["/data/outbox/a.png"],
                "checks": [{"id": "unit", "argv": ["pytest"], "cwd": ".", "scope": "focused", "inputs": ["x"]}]}
        text = turns.render(data)
        self.assertEqual(text.count("NEED_USER_INPUT:"), 1)
        self.assertTrue(text.endswith("NEED_USER_INPUT: Which store?\n1. SQLite\n2. Postgres"))
        self.assertIn("ATTACH: /data/outbox/a.png", text)
        plan = json.loads(text.split("CHECK_PLAN: ", 1)[1].splitlines()[0])
        self.assertEqual(plan["checks"][0]["id"], "unit")

    def test_gate_and_review_render_the_exact_legacy_contracts(self):
        gate = turns.render({"status": "done", "summary": "ok", "verification": {
            "status": "pass", "commands": ["pytest: pass"], "openspec": "pass", "tasks": "3/3"}})
        self.assertEqual(main.parse_quality_gate(gate)[1], None)
        review = turns.render({"status": "done", "summary": "ok", "review": {
            "critical": 0, "important": 0, "tests": ["pytest: pass"]}})
        self.assertEqual(main.parse_internal_review(review)[1], None)
        failing = turns.render({"status": "done", "summary": "ok", "review": {"critical": 1, "important": 0}})
        self.assertIsNone(main.parse_internal_review(failing)[0])

    def test_threads_render_as_resolve_lines(self):
        text = turns.render({"status": "done", "summary": "ok", "threads": [
            {"id": "PRRT_1", "action": "fixed", "reply": "renamed\nthe helper"}]})
        self.assertIn("RESOLVE: PRRT_1 fixed: renamed the helper", text)

    def test_extract_finds_the_last_valid_object_in_prose_and_fences(self):
        text = 'Done.\n```json\n{"status": "done", "summary": "first"}\n```\nthen {"status": "blocked", "summary": "last"}'
        self.assertEqual(turns.extract(text)["summary"], "last")
        self.assertIsNone(turns.extract("no object here {not json}"))
        self.assertIsNone(turns.extract('{"status": "maybe", "summary": "x"}'))

    def test_finalize_falls_back_to_the_raw_text(self):
        self.assertEqual(turns.finalize("plain prose"), ("plain prose", None))
        output, data = turns.finalize("ignored", {"status": "done", "summary": "structured"})
        self.assertEqual((output, data["summary"]), ("structured", "structured"))


if __name__ == "__main__":
    unittest.main()


class ReviewGuide(unittest.TestCase):
    def test_superpowers_template_is_filled_or_falls_back(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            skill = Path(tmp) / "skills/requesting-code-review"
            skill.mkdir(parents=True)
            (skill / "code-reviewer.md").write_text("Review [DESCRIPTION] against [PLAN_OR_REQUIREMENTS] "
                                                    "between [BASE_SHA] and [HEAD_SHA].")
            state = {"item": "Export CSV", "slug": "export-csv", "base_sha": "abc123"}
            with patch.object(main.config, "SUPERPOWERS_PLUGIN_DIR", Path(tmp)):
                guide = main._review_guide(state)
            self.assertIn("Review Export CSV against openspec/changes/export-csv/ between abc123 and HEAD.", guide)
            with patch.object(main.config, "SUPERPOWERS_PLUGIN_DIR", Path(tmp) / "missing"):
                self.assertEqual(main._review_guide(state), prompts.REVIEW_GUIDE_FALLBACK)
        rendered = main._internal_review_prompt({"slug": "s", "base_sha": "abc", "item": "x"})
        self.assertNotRegex(rendered, r"\$[a-z_]+")
        self.assertIn("do not dispatch another reviewer", rendered)


class TurnRequest(unittest.TestCase):
    def test_facts_are_a_short_deterministic_yaml_block(self):
        state = {"item": "Exportar CSV", "task_language": "Spanish", "branch": "b", "base_sha": "abc",
                 "slug": "export-csv", "implement_budget": 5, "implementation_turn": 1,
                 "implementation_progress": {"pending": ["2.1 API"]}}
        block = turns.request(state, "IMPLEMENTING")
        self.assertEqual(block, turns.request(dict(state), "IMPLEMENTING"))
        self.assertTrue(block.startswith("```yaml\nphase: \"IMPLEMENTING\"\ntask: \"Exportar CSV\""))
        for line in ('skill: "openspec-apply-change, test-driven-development"', 'turn: "2 of 5"',
                     'pending_tasks: ["2.1 API"]', 'language: "Spanish"'):
            self.assertIn(line, block)
        self.assertNotIn("pending_tasks", turns.request(state, "INTERNAL_REVIEW"))
        self.assertEqual(turns.request({}, None), "")
