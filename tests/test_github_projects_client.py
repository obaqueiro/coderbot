"""GitHub Projects v2 backend, against a fake `gh` that answers canned payloads."""
import json
import subprocess
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch

import config
import github_projects_client as ghp

ME = config.INSTANCE_ID
REPO = "acme/project"
PROJECT = {"repositoryOwner": {"projectV2": {
    "id": "PVT_1", "title": "Board",
    "fields": {"nodes": [
        {"id": "F_status", "name": "Status", "options": [
            {"id": "O_backlog", "name": "Backlog"}, {"id": "O_ready", "name": "Ready"},
            {"id": "O_prog", "name": "In progress"}, {"id": "O_rev", "name": "In review"},
            {"id": "O_done", "name": "Done"}]},
        {"id": "F_prio", "name": "Priority", "options": [
            {"id": "P0", "name": "P0"}, {"id": "P1", "name": "P1"}]},
        {}]}}}}


def issue(item_id, number, title, status, labels=(), body="", repo=REPO, state="OPEN",
          priority=None, kind="Issue"):
    values = [{"name": status, "field": {"name": "Status"}}]
    if priority:
        values.append({"name": priority, "field": {"name": "Priority"}})
    content = {"__typename": kind}
    if kind == "Issue":
        content.update(id=f"I_{number}", number=number, title=title, body=body,
                       url=f"https://github.com/{repo}/issues/{number}", state=state,
                       labels={"nodes": [{"name": l} for l in labels]},
                       repository={"nameWithOwner": repo})
    elif kind == "DraftIssue":
        content.update(id=f"DI_{number}", title=title, body=body)
    return {"id": item_id, "fieldValues": {"nodes": values}, "content": content}


class FakeGh:
    """subprocess.run stand-in: routes each gh invocation to a canned reply and
    records the calls. `items` is the board; label add/remove mutate it in place so
    re-reads observe the change (unless `rival` injects a competing label)."""

    def __init__(self, items, rival=None):
        self.items = items
        self.rival = rival
        self.calls = []
        self.fail = {}

    def by_id(self, item_id):
        return next((i for i in self.items if i["id"] == item_id), None)

    def by_number(self, number):
        return next((i for i in self.items if i["content"].get("number") == int(number)), None)

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        args = cmd[1:]
        out = self.route(args)
        return subprocess.CompletedProcess(cmd, 0, out, "")

    def route(self, args):
        if args[0] == "api" and args[1] == "graphql":
            query = args[3][len("query="):]
            var = dict(a.split("=", 1) for a in args[4:] if not a.startswith("-"))
            if "repositoryOwner" in query:
                return json.dumps({"data": PROJECT})
            if "items(first: 100" in query:
                return json.dumps({"data": {"node": {"items": {
                    "pageInfo": {"hasNextPage": False}, "nodes": self.items}}}})
            if "ProjectV2Item {" in query:
                return json.dumps({"data": {"node": self.by_id(var["id"])}})
            if "updateProjectV2ItemFieldValue" in query:
                item = self.by_id(var["itemId"])
                name = next(o["name"] for o in PROJECT["repositoryOwner"]["projectV2"]["fields"]
                            ["nodes"][0]["options"] if o["id"] == var["optionId"])
                if item:
                    item["fieldValues"]["nodes"][0]["name"] = name
                return json.dumps({"data": {"updateProjectV2ItemFieldValue": {}}})
            if "addProjectV2ItemById" in query:
                return json.dumps({"data": {"addProjectV2ItemById": {"item": {"id": "PVTI_new"}}}})
            raise AssertionError(f"unexpected graphql: {query[:60]}")
        if args[0] == "api" and args[1] == "-X":
            number = args[3].split("/")[4]
            item = self.by_number(number)
            labels = item["content"]["labels"]["nodes"]
            if args[2] == "POST":
                label = args[5][len("labels[]="):]
                labels.append({"name": label})
                if self.rival:
                    labels.append({"name": self.rival})
            else:
                label = urllib.parse.unquote(args[3].rsplit("/", 1)[1])
                labels[:] = [l for l in labels if l["name"] != label]
            return "[]"
        if args[0] == "label":
            return ""
        if args[0] == "issue" and args[1] == "create":
            return "https://github.com/acme/project/issues/77\n"
        if args[0] == "issue" and args[1] == "view":
            return json.dumps({"id": "I_77"})
        if args[0] == "issue" and args[1] == "comment":
            return ""
        raise AssertionError(f"unexpected gh call: {args}")

    def statuses(self):
        return [c for c in self.calls if "updateProjectV2ItemFieldValue" in " ".join(c)]

    def label_calls(self):
        return [(c[3], c[4]) for c in self.calls if c[1:3] == ["api", "-X"]]


class Base(unittest.TestCase):
    def setUp(self):
        ghp._cache.clear()
        ghp._cache["repo"] = REPO
        patches = [patch.object(config, "GH_PROJECT_OWNER", "acme"),
                   patch.object(config, "GH_PROJECT_NUMBER", 3),
                   patch.object(config, "GH_PROJECT_PICK_STATUSES", ["Ready"]),
                   patch.object(ghp, "_download_images", return_value=[])]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def gh(self, items, rival=None):
        fake = FakeGh(items, rival)
        p = patch.object(ghp.operations, "tool", side_effect=fake)
        p.start()
        self.addCleanup(p.stop)
        return fake


class Listing(Base):
    def test_only_ready_open_issues_of_target_repo_without_labels_are_pending(self):
        self.gh([
            issue("A", 1, "Ready one", "Ready"),
            issue("B", 2, "Backlog one", "Backlog"),
            issue("C", 3, "Foreign", "Ready", labels=["codebot:other"]),
            issue("D", 4, "Held", "Ready", labels=[ghp.hold_label()]),
            issue("E", 5, "Mine in progress", "In progress", labels=[ghp.claim_label()]),
            issue("F", 6, "Mine done", "Done", labels=[ghp.claim_label()]),
            issue("G", 7, "Draft", "Ready", kind="DraftIssue"),
            issue("H", 8, "Elsewhere", "Ready", repo="acme/other"),
            issue("I", 9, "Closed", "Ready", state="CLOSED"),
            issue("J", 10, "PR", "Ready", kind="PullRequest"),
        ])
        items = ghp.list_pending_items()
        self.assertEqual([(i["id"], i["claimed_by_me"]) for i in items],
                         [("A", False), ("E", True)])
        self.assertEqual(items[0]["url"], "https://github.com/acme/project/issues/1")
        self.assertEqual(items[0]["text"], "Ready one")

    def test_pick_statuses_are_configurable_and_case_insensitive(self):
        self.gh([issue("A", 1, "a", "Ready"), issue("B", 2, "b", "Backlog")])
        with patch.object(config, "GH_PROJECT_PICK_STATUSES", ["backlog", "READY"]):
            self.assertEqual([i["id"] for i in ghp.list_pending_items()], ["A", "B"])

    def test_priority_from_title_tag_then_priority_field(self):
        self.gh([issue("A", 1, "Codebot[2] tagged", "Ready", priority="P1"),
                 issue("B", 2, "field only", "Ready", priority="P1"),
                 issue("C", 3, "none", "Ready")])
        self.assertEqual([i["priority"] for i in ghp.list_pending_items()], [2, 2, None])

    def test_detail_is_body_and_images_are_extracted(self):
        body = "Do it.\n\n![shot](https://github.com/user-attachments/assets/x.png)\n" \
               '<img src="https://example.com/a.jpg"> ![dup](https://example.com/a.jpg)'
        self.assertEqual(ghp.image_urls(body),
                         ["https://github.com/user-attachments/assets/x.png",
                          "https://example.com/a.jpg"])
        self.gh([issue("A", 1, "t", "Ready", body=body)])
        self.assertEqual(ghp.list_pending_items()[0]["detail"], body.strip())


class Claiming(Base):
    def test_same_name_different_fingerprint_is_foreign(self):
        other = f"{config.GH_LABEL_PREFIX}:{config.INSTANCE_ID}@{'f' * 32}"
        if other == ghp.claim_label():
            other = f"{config.GH_LABEL_PREFIX}:{config.INSTANCE_ID}@{'e' * 32}"
        fake = self.gh([issue("A", 1, "task", "Ready", labels=[other])])
        self.assertFalse(ghp.claim_task("task", "A"))
        self.assertEqual(fake.label_calls(), [])
        self.assertEqual(ghp.list_pending_items(), [])

    def test_foreign_hold_with_matching_readable_name_is_not_removed(self):
        other = f"{config.GH_LABEL_PREFIX}-hold:{config.INSTANCE_ID}@{'f' * 32}"
        if other == ghp.hold_label():
            other = f"{config.GH_LABEL_PREFIX}-hold:{config.INSTANCE_ID}@{'e' * 32}"
        fake = self.gh([issue("A", 1, "task", "In progress", labels=[other])])
        self.assertFalse(ghp.hold_task("task", "A"))
        self.assertFalse(ghp.unhold_task("task", "A"))
        self.assertEqual(fake.label_calls(), [])

    def test_legacy_marker_requires_local_state_before_migration(self):
        legacy = f"{config.GH_LABEL_PREFIX}:{config.INSTANCE_ID}"
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(config, "STATE_PATH", Path(tmp) / "state.json"), \
                patch.object(config, "HOLDS_PATH", Path(tmp) / "holds.json"):
            fake = self.gh([issue("A", 1, "task", "In progress", labels=[legacy])])
            self.assertFalse(ghp.claim_task("task", "A"))
            self.assertEqual(fake.label_calls(), [])
            (Path(tmp) / "state.json").write_text(json.dumps({"item_id": "A", "item": "task"}))
            self.assertTrue(ghp.claim_task("task", "A"))
            self.assertEqual({label["name"] for label in fake.items[0]["content"]["labels"]["nodes"]},
                             {ghp.claim_label()})

    def test_claim_adds_label_verifies_and_moves_to_in_progress(self):
        fake = self.gh([issue("A", 1, "task", "Ready")])
        self.assertTrue(ghp.claim_task("task", "A"))
        self.assertEqual(fake.label_calls(), [("POST", f"repos/{REPO}/issues/1/labels")])
        self.assertEqual(len(fake.statuses()), 1)
        self.assertEqual(fake.items[0]["fieldValues"]["nodes"][0]["name"], "In progress")

    def test_claim_by_title_when_no_id(self):
        fake = self.gh([issue("A", 1, "task", "Done", labels=[]),
                        issue("B", 2, "task", "Ready")])
        self.assertTrue(ghp.claim_task("Task ", None))
        self.assertEqual(fake.label_calls()[0], ("POST", f"repos/{REPO}/issues/2/labels"))

    def test_foreign_claim_is_refused_without_writes(self):
        fake = self.gh([issue("A", 1, "task", "Ready", labels=["codebot:other"])])
        self.assertFalse(ghp.claim_task("task", "A"))
        self.assertEqual(fake.label_calls(), [])
        self.assertEqual(fake.statuses(), [])

    def test_own_claim_is_idempotent(self):
        fake = self.gh([issue("A", 1, "task", "In progress", labels=[ghp.claim_label()])])
        self.assertTrue(ghp.claim_task("task", "A"))
        self.assertEqual(fake.label_calls(), [])
        self.assertEqual(fake.statuses(), [])

    def test_lost_race_removes_own_label_and_leaves_status(self):
        fake = self.gh([issue("A", 1, "task", "Ready")], rival="codebot:other")
        self.assertFalse(ghp.claim_task("task", "A"))
        self.assertEqual([c[3] for c in fake.calls if c[1] == "api" and c[2] == "-X"],
                         ["POST", "DELETE"])
        self.assertEqual(fake.statuses(), [])
        self.assertEqual({l["name"] for l in fake.items[0]["content"]["labels"]["nodes"]},
                         {"codebot:other"})

    def test_done_or_missing_item_cannot_be_claimed(self):
        self.gh([issue("A", 1, "task", "Done")])
        self.assertFalse(ghp.claim_task("task", "A"))
        self.assertFalse(ghp.claim_task("nope", None))


class Transitions(Base):
    def test_unclaim_removes_label_and_returns_to_ready(self):
        fake = self.gh([issue("A", 1, "task", "In progress", labels=[ghp.claim_label()])])
        self.assertTrue(ghp.unclaim_task("task", "A"))
        self.assertEqual(fake.items[0]["fieldValues"]["nodes"][0]["name"], "Ready")
        self.assertEqual(fake.items[0]["content"]["labels"]["nodes"], [])

    def test_hold_swaps_labels_and_keeps_status(self):
        fake = self.gh([issue("A", 1, "task", "In progress", labels=[ghp.claim_label()])])
        self.assertTrue(ghp.hold_task("task", "A"))
        self.assertEqual({l["name"] for l in fake.items[0]["content"]["labels"]["nodes"]},
                         {ghp.hold_label()})
        self.assertEqual(fake.statuses(), [])
        self.assertTrue(ghp.unhold_task("task", "A"))
        self.assertEqual(fake.items[0]["content"]["labels"]["nodes"], [])

    def test_mark_done_sets_done_and_clears_labels(self):
        fake = self.gh([issue("A", 1, "task", "In review", labels=[ghp.claim_label()])])
        self.assertTrue(ghp.mark_done("task", "A"))
        self.assertEqual(fake.items[0]["fieldValues"]["nodes"][0]["name"], "Done")
        self.assertEqual(fake.items[0]["content"]["labels"]["nodes"], [])

    def test_mark_done_on_done_item_is_success_without_status_write(self):
        fake = self.gh([issue("A", 1, "task", "Done")])
        self.assertTrue(ghp.mark_done("task", "A"))
        self.assertEqual(fake.statuses(), [])

    def test_note_pr_moves_to_review_without_commenting(self):
        fake = self.gh([issue("A", 1, "task", "In progress", labels=[ghp.claim_label()])])
        ghp.note_pr("task", "A", "https://github.com/acme/project/pull/9")
        self.assertEqual(fake.items[0]["fieldValues"]["nodes"][0]["name"], "In review")
        self.assertFalse(any(c[1:3] == ["issue", "comment"] for c in fake.calls))

    def test_note_activity_comments_on_the_issue_and_returns_no_ref(self):
        fake = self.gh([issue("A", 1, "task", "In progress", labels=[ghp.claim_label()])])
        self.assertIsNone(ghp.note_activity("task", "A", "[bot] Picked\n\nbecause", ref=None))
        comment = next(c for c in fake.calls if c[1:3] == ["issue", "comment"])
        self.assertEqual(comment[3], "1")
        self.assertEqual(comment[-1], "[bot] Picked\n\nbecause")
        self.assertIsNone(ghp.note_activity("missing", None, "x"))


class Seeding(Base):
    def test_ensure_item_is_noop_when_title_or_body_matches(self):
        fake = self.gh([issue("A", 1, "Add e2e harness", "Done"),
                        issue("B", 2, "short title", "Ready", body="Long seeded text here.")])
        self.assertFalse(ghp.ensure_item("add e2e  harness"))
        self.assertFalse(ghp.ensure_item("Long seeded text here."))
        self.assertFalse(any(c[1:3] == ["issue", "create"] for c in fake.calls))

    def test_ensure_item_creates_issue_adds_to_board_in_ready(self):
        fake = self.gh([])
        long = "Add a Code Review workflow. " + "Details " * 60
        self.assertTrue(ghp.ensure_item(long))
        create = next(c for c in fake.calls if c[1:3] == ["issue", "create"])
        self.assertEqual(create[create.index("--title") + 1], "Add a Code Review workflow.")
        self.assertEqual(create[create.index("--body") + 1], long.strip())
        self.assertTrue(any("addProjectV2ItemById" in " ".join(c) for c in fake.calls))
        status = fake.statuses()[0]
        self.assertIn("optionId=O_ready", status)
        self.assertIn("itemId=PVTI_new", status)

    def test_short_seed_uses_text_as_title(self):
        self.assertEqual(ghp._split_seed("Short item"), ("Short item", ""))


class Validation(Base):
    def test_validate_checks_statuses_and_creates_labels(self):
        fake = self.gh([])
        ghp.validate()
        labels = [c for c in fake.calls if c[1:3] == ["label", "create"]]
        self.assertEqual([c[3] for c in labels], [ghp.claim_label(), ghp.hold_label()])
        self.assertIn("--force", labels[0])

    def test_validate_reports_missing_status_with_actual_options(self):
        self.gh([])
        with patch.object(config, "GH_PROJECT_DONE_STATUS", "Shipped"):
            with self.assertRaisesRegex(RuntimeError, r"\['Shipped'\].*Backlog"):
                ghp.validate()

    def test_scope_errors_carry_the_project_scope_hint(self):
        failing = subprocess.CompletedProcess([], 1, "", "GraphQL: INSUFFICIENT_SCOPES ...")
        with patch.object(ghp.operations, "tool", return_value=failing):
            with self.assertRaisesRegex(RuntimeError, "'project' scope"):
                ghp._project()

    def test_target_repo_from_origin_remote(self):
        ghp._cache.clear()
        for url in ("git@github.com:acme/project.git", "https://github.com/acme/project",
                    "https://github.com/acme/project.git", "git@github-work:acme/project.git",
                    "ssh://git@github.com/acme/project.git"):
            ghp._cache.pop("repo", None)
            with patch.object(ghp.operations, "tool",
                              return_value=subprocess.CompletedProcess([], 0, url + "\n", "")):
                self.assertEqual(ghp._target_repo(), "acme/project", url)

    def test_describe(self):
        self.assertEqual(ghp.describe(), "project=acme/3")


if __name__ == "__main__":
    unittest.main()
