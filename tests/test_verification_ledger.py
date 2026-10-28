from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import check_plan
import final_checks
import verification_ledger
import handoffs


class VerificationLedgerTests(unittest.TestCase):
    def test_requirement_inventory_includes_all_scenarios(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "openspec/changes/feature/specs/models/spec.md"
            path.parent.mkdir(parents=True)
            path.write_text("### Requirement: Model selector\nMust select.\n#### Scenario: Persist selection\n"
                            "### Requirement: Effort\nMust set effort.\n#### Scenario: Next turn\n")
            values = verification_ledger.requirements(root, {"slug": "feature"})
            self.assertEqual([v["title"] for v in values], ["Model selector", "Effort"])
            self.assertEqual(values[0]["scenarios"], ["Persist selection"])

    def test_waiver_skips_only_general_collection_and_never_claims_pass(self):
        state = {"check_waivers": [], "has_e2e_harness": True}
        verification_ledger.waiver(state, "Skip broken general E2E", "e2e:general", "head")
        verification_ledger.waiver(state, "Skip broken general E2E", "e2e:general", "head")
        self.assertEqual(len(state["check_waivers"]), 1)
        plan = [check_plan.Check("unit:backend", ["test"]), check_plan.Check("e2e:general", ["e2e"])]
        with patch.object(final_checks.check_plan, "discover", return_value=plan), \
             patch.object(final_checks.checks, "execute_plan", return_value=[{"status": "pass"}]) as execute, \
             patch.object(final_checks, "snapshot", return_value="head"):
            result = final_checks.run(state, Path("/repo"), type("Store", (), {
                "task_identity": lambda *args: "task", "list": lambda *args, **kwargs: []})())
        self.assertEqual([c.id for c in execute.call_args.args[0]], ["unit:backend"])
        self.assertIn("waived", handoffs.concise_verification(state))
        self.assertEqual(result["status"], "pass")
