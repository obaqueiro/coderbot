"""End-to-end walk of workspace mode through the real tick loop: a non-Git root with
children `api`, `ui` and `unused`, each with a bare origin behind a GitHub-looking URL
(git `insteadOf`). Fake `claude`/`gh` (tests/workspace_walk/bin), a post-receive hook
logging every push (tests/workspace_walk/hooks) and the FSM walk's channel/backlog driver
(tests/fsm_walk/driver.py). Faults are opt-in WALK_* knobs documented in those fakes.
Never touches the real data/."""
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.test_fsm_walk import _openspec_version, git

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "tests" / "workspace_walk" / "bin"
HOOK = ROOT / "tests" / "workspace_walk" / "hooks" / "post-receive"
ITEM = "Greet users from the API in the UI"
SLUG = "greet-users-from-the-api-in-the-ui"
REPLIES = [["Do you authorize implementing", "approve"], ["PRs ready for review", "merge"],
           ["closed without merging", "retry"]]


@unittest.skipUnless((_openspec_version() or (0,)) >= (1, 9, 0), "needs the pinned openspec >= 1.9.0")
class WorkspaceWalk(unittest.TestCase):
    def fixture(self, **extra):
        tmp = Path(tempfile.mkdtemp(prefix="ws-walk-") if os.environ.get("WALK_KEEP")
                   else self.enterContext(tempfile.TemporaryDirectory()))
        root, data, home = tmp / "ws", tmp / "data", tmp / "home"
        for path in (root / "openspec/specs", root / "openspec/changes", data, home, tmp / "origins"):
            path.mkdir(parents=True)
        env = {"PATH": f"{BIN}{os.pathsep}{os.environ['PATH']}", "HOME": str(home),
               "GIT_AUTHOR_NAME": "walk", "GIT_AUTHOR_EMAIL": "walk@example.com",
               "GIT_COMMITTER_NAME": "walk", "GIT_COMMITTER_EMAIL": "walk@example.com"}
        # cwd=tmp: the checkout under test may be a worktree whose .git is not mounted.
        run = lambda *args: subprocess.run(["git", *args], env={**os.environ, **env}, cwd=tmp, check=True,
                                           capture_output=True)
        self.origins = {}
        for name in ("api", "ui", "unused"):
            origin, url, child = tmp / "origins" / f"{name}.git", f"https://github.com/acme/{name}.git", root / name
            run("init", "-q", "--bare", "-b", "main", str(origin))
            run("config", "--global", f"url.{origin}.insteadOf", url)  # GitHub-looking URL, local bare origin
            run("clone", "-q", url, str(child))
            (child / "README.md").write_text(f"# {name}\n")
            run("-C", str(child), "add", "-A")
            run("-C", str(child), "commit", "-qm", "initial")
            run("-C", str(child), "push", "-q", "origin", "main")
            run("-C", str(child), "remote", "set-head", "origin", "main")
            shutil.copy2(HOOK, origin / "hooks" / "post-receive")  # logs every later push
            self.origins[name] = origin
        if extra.get("WALK_DROP_HEAD"):  # an unrelated child with no discoverable base branch
            git(root / extra["WALK_DROP_HEAD"], "symbolic-ref", "--delete", "refs/remotes/origin/HEAD")
        (root / "workspace.json").write_text(json.dumps({"merge_order": ["ui", "api"]}))
        self.initial_unused = git(tmp, "--git-dir", str(self.origins["unused"]), "rev-parse", "main")
        env.update({"WALK_DIR": str(tmp), "WALK_ITEM": ITEM, "WALK_SLUG": SLUG,
                    "WALK_REPLIES": json.dumps(REPLIES), "WALK_REPLY_EACH": "1",
                    "WALK_ORIGIN": str(self.origins["api"]),
                    "CODEBOT_REPO_PATH": str(root), "CODEBOT_DATA_DIR": str(data),
                    "CODEBOT_WORKSPACE_MANIFEST": "workspace.json",
                    "CODEBOT_TASK_SOURCE": "gdoc", "CODEBOT_DOC_ID": "walk", "CODEBOT_COMM_CHANNEL": "email",
                    "CODEBOT_USER_EMAIL": "walk@example.com", "CODEBOT_AGENT": "claude",
                    "CODEBOT_INSTANCE": "walkbot", "CODEBOT_POLL_INTERVAL": "1",
                    # The guard under test, not the copy baked into the image at build time.
                    "CODEBOT_BRIDGE_PLUGIN_DIR": str(ROOT / "agent-plugin"),
                    "CODEBOT_EVIDENCE_UPLOAD": "off", "CODEBOT_ACTIVITY_TRAIL": "off",
                    "CODEBOT_SELF_HEAL_CODE_REVIEW": "off", "CODEBOT_LOG_LEVEL": "INFO", "GH_TOKEN": "walk",
                    "PYTHONDONTWRITEBYTECODE": "1", **extra})
        self.root, self.tmp, self.env, self.stderr = root, tmp, env, ""
        return env

    def drive(self, **extra):
        """One coderbot process over the fixture; a later call is a restart on the same data."""
        (self.tmp / "walk.json").unlink(missing_ok=True)
        proc = subprocess.run([sys.executable, str(ROOT / "tests/fsm_walk/driver.py")], env={**self.env, **extra},
                              cwd=ROOT, capture_output=True, text=True, timeout=600)
        self.stderr += f"\n--- run exited {proc.returncode}\n" + proc.stderr
        result = json.loads((self.tmp / "walk.json").read_text()) if (self.tmp / "walk.json").exists() else {}
        logs = {name: (self.tmp / name).read_text() if (self.tmp / name).exists() else ""
                for name in ("claude.log", "gh.log", "gh-unsupported.log", "pushes.log", "guard.log")}
        saved = self.tmp / "data/state.json"
        saved = json.loads(saved.read_text()) if saved.exists() else {}
        unknown = sorted({p.read_text()[:300] + " … " + p.read_text()[-300:] for p in self.tmp.glob("unknown-prompt-*.txt")})
        context = (f"\nfixture={self.tmp}\noutcome={result.get('outcome')!r} states={result.get('states')}\n"
                   f"sent={[m['subject'] for m in result.get('sent', [])]}\n"
                   f"stuck={[m['body'] for m in result.get('sent', []) if 'stuck' in m['subject'] or 'blocked' in m['subject']]}\n"
                   f"saved state={ {k: v for k, v in saved.items() if v and k != 'workspace'} }\n"
                   f"unknown prompts={unknown}\ngh unsupported={logs['gh-unsupported.log']}\n"
                   f"events:\n{self.events()}\nstderr tail:\n{self.stderr[-6000:]}")
        return proc, result, logs, context

    def events(self):
        """Transitions, warnings and errors, each distinct message once (retry loops repeat)."""
        seen, lines = set(), []
        for line in self.stderr.splitlines():
            if line.startswith("--- run") or " WARNING " in line or " ERROR " in line or "state transition" in line:
                message = line[24:]
                if message not in seen:
                    seen.add(message)
                    lines.append(line[11:300])
        return "\n".join(lines[-80:])

    def walk(self, **extra):
        self.fixture(**extra)
        _, result, logs, context = self.drive()
        return result, logs, context

    def calls(self, logs, *command):
        calls = [json.loads(line) for line in logs["gh.log"].splitlines()]
        return [c for c in calls if c[:len(command)] == list(command)]

    def repo_of(self, call):
        return call[call.index("--repo") + 1].split("/")[-1] if "--repo" in call else None

    def merged(self):
        return json.loads((self.tmp / "gh.json").read_text()).get("merges", [])

    def origin_log(self, name):
        return git(self.tmp, "--git-dir", str(self.origins[name]), "log", "--format=%s", "main")

    def sent(self, result, subject):
        return [m for m in result.get("sent", []) if m["subject"].endswith(subject)]

    def agent(self, logs):
        return [entry for entry in map(json.loads, logs["claude.log"].splitlines()) if not entry["utility"]]

    def test_feature_walks_across_repositories_to_ordered_merges(self):
        result, logs, context = self.walk()
        self.assertEqual(result.get("outcome"), "merged", context)
        self.assertEqual(result["done"], [ITEM], context)
        self.assertEqual(logs["gh-unsupported.log"], "", context)
        calls = self.calls(logs)
        creates = self.calls(logs, "pr", "create")
        self.assertEqual(sorted(self.repo_of(c) for c in creates), ["api", "ui"], context)
        self.assertTrue(all("--draft" in c for c in creates), context)  # lazy drafts during implementation
        heads = {c[c.index("--head") + 1] for c in creates}
        self.assertEqual(len(heads), 1, context)
        self.assertRegex(heads.pop(), rf"^walkbot-{SLUG}-[0-9a-f]{{6}}$")  # a unique branch per attempt
        merges = [self.repo_of(c) for c in calls if c[:2] == ["pr", "merge"]]
        self.assertEqual(merges, ["ui", "api"], context)  # the manifest's order
        ready = [i for i, c in enumerate(calls) if c[:2] == ["pr", "ready"]]
        first_merge = next(i for i, c in enumerate(calls) if c[:2] == ["pr", "merge"])
        self.assertEqual(len(ready), 2, context)
        self.assertLess(max(ready), first_merge, context)
        self.assertNotIn("unused", {self.repo_of(c) for c in calls}, context)
        self.assertIn("Add greet() with a unit test", self.origin_log("api"), context)
        self.assertEqual(git(self.tmp, "--git-dir", str(self.origins["unused"]), "rev-parse", "main"),
                         self.initial_unused, context)
        for name in ("api", "ui", "unused"):
            self.assertEqual(git(self.root / name, "branch", "--show-current"), "main", context)
        self.assertEqual(git(self.root / "unused", "branch", "--list"), "* main", context)
        subjects = [m["subject"].split(" — ")[-1] for m in result["sent"]]
        self.assertIn("draft PR", subjects, context)
        self.assertFalse(any("stuck" in s for s in subjects), context)
        working = self.agent(logs)
        self.assertEqual([a["phase"] for a in working], ["EXPLORE", "PROPOSE", "IMPLEMENT", "REVIEW"], context)
        self.assertTrue(all(a["gh_token"] == "" and a["credential_helper"] == "" for a in working), context)

    def test_closed_pr_is_reopened_after_retry_without_touching_the_other(self):
        result, logs, context = self.walk(WALK_CLOSE="ui")
        self.assertEqual(result.get("outcome"), "merged", context)
        creates = [self.repo_of(c) for c in self.calls(logs, "pr", "create")]
        self.assertEqual(sorted(creates), ["api", "ui", "ui"], context)
        self.assertTrue(any("closed without merging" in m["body"] for m in result["sent"]), context)
        self.assertEqual(sorted(self.repo_of(c) for c in self.calls(logs, "pr", "merge")), ["api", "ui"], context)

    def test_a_child_losing_origin_head_does_not_stop_the_feature(self):
        result, logs, context = self.walk(WALK_DROP_HEAD="unused")
        self.assertEqual(result.get("outcome"), "merged", context)

    def test_a_kill_after_each_side_effect_restarts_without_duplicates(self):
        # create: the draft PR exists but coderbot never recorded its number; push: the
        # first feature branch landed before its PR was opened; merge: `ui` merged but
        # the merge was never recorded. Each restart reuses what is already there.
        for point in ("create", "push", "merge"):
            with self.subTest(kill=point):
                self.fixture(WALK_KILL=point)
                killed, _, _, context = self.drive()
                self.assertEqual(killed.returncode, -signal.SIGKILL, context)
                # The restarted process has no record of the merge question asked before the kill.
                _, result, logs, context = self.drive(
                    WALK_REPLY_AFTER=json.dumps([["WAIT_MERGE", "WAIT_MERGE", "merge"]]) if point == "merge" else "")
                self.assertEqual(result.get("outcome"), "merged", context)
                creates = sorted(self.repo_of(c) for c in self.calls(logs, "pr", "create"))
                self.assertEqual(creates, ["api", "ui"], context)  # no duplicate PR
                self.assertEqual([self.repo_of(c) for c in self.calls(logs, "pr", "merge")], ["ui", "api"], context)
                self.assertEqual(self.merged(), ["ui", "api"], context)  # each merged exactly once
                pushes = [json.loads(line) for line in logs["pushes.log"].splitlines()]
                feature = [(p["repo"], p["new"]) for p in pushes if p["ref"].startswith("refs/heads/walkbot-")]
                self.assertEqual(sorted(name for name, _ in feature), ["api", "ui"], context)  # one push each
                self.assertIn("Add greet() with a unit test", self.origin_log("api"), context)

    def test_a_partial_merge_is_recorded_reported_and_retried(self):
        result, logs, context = self.walk(WALK_MERGE_FAIL="api", WALK_REPLIES=json.dumps(
            REPLIES + [["partial merge", "merge"]]))
        self.assertEqual(result.get("outcome"), "merged", context)
        [partial] = self.sent(result, "partial merge")
        self.assertIn("Merged in order: ui. api failed", partial["body"], context)
        self.assertIn("Partial merge: merged ui; api failed", partial["body"], context)
        self.assertEqual([self.repo_of(c) for c in self.calls(logs, "pr", "merge")], ["ui", "api", "api"], context)
        self.assertEqual(self.merged(), ["ui", "api"], context)  # ui is not merged again on the retry

    def test_an_external_push_to_a_child_pr_is_taken_in_and_reverified(self):
        # The owner says "merge" once the re-verified PRs wait again. (A "merge" sent while
        # coderbot re-verifies is routed to the lateral conversation in workspace mode.)
        result, logs, context = self.walk(WALK_EXTERNAL="api", WALK_REPLIES=json.dumps(REPLIES[:1]),
                                          WALK_REPLY_AFTER=json.dumps([[["WAIT_MERGE", "VERIFYING"], "WAIT_MERGE", "merge"]]))
        self.assertEqual(result.get("outcome"), "merged", context)
        self.assertTrue(self.sent(result, "PR updated externally"), context)
        states = result["states"]
        self.assertIn("VERIFYING", states[states.index("WAIT_MERGE"):], context)
        self.assertEqual([a["phase"] for a in self.agent(logs)].count("REVIEW"), 2, context)  # reviewed again
        self.assertIn("Reviewer tweak", self.origin_log("api"), context)
        self.assertEqual(self.merged(), ["ui", "api"], context)

    def test_a_conflict_after_the_first_merge_is_resolved_then_merged(self):
        result, logs, context = self.walk(WALK_CONFLICT="api",
                                          WALK_REPLY_AFTER=json.dumps([["RESOLVE_CONFLICTS", "WAIT_MERGE", "merge"]]))
        self.assertEqual(result.get("outcome"), "merged", context)
        [partial] = self.sent(result, "partial merge")
        self.assertIn("Merged in order: ui. api failed", partial["body"], context)
        self.assertIn("RESOLVE_CONFLICTS", result["states"], context)
        self.assertEqual(self.merged(), ["ui", "api"], context)
        self.assertIn("Someone else's greet", self.origin_log("api"), context)
        self.assertIn("Hello", git(self.tmp, "--git-dir", str(self.origins["api"]), "show", "main:greet.py"), context)

    def test_a_child_appearing_mid_task_is_baselined_not_adopted(self):
        result, logs, context = self.walk(WALK_NEW_CHILD="extra")
        self.assertEqual(result.get("outcome"), "merged", context)
        self.assertTrue((self.root / "extra/.git").is_dir(), context)
        self.assertNotIn("extra", {self.repo_of(c) for c in self.calls(logs)}, context)
        self.assertEqual(git(self.root / "extra", "branch", "--list"), "* main", context)
        self.assertEqual(self.merged(), ["ui", "api"], context)

    def test_the_guard_denies_git_init_at_the_root_and_controller_actions(self):
        result, logs, context = self.walk(WALK_GUARD="1")
        probes = [json.loads(line) for line in logs["guard.log"].splitlines()]
        self.assertEqual(len(probes), 9, context)
        for probe in probes:
            self.assertEqual(probe["decision"], probe["expected"], f"{probe}{context}")
            self.assertEqual(probe["exit"], 0, context)  # exit 2 would block every call
        self.assertFalse((self.root / ".git").exists(), context)
        self.assertEqual(result.get("outcome"), "merged", context)

    def test_dirty_abort_waits_clean_then_repicks_the_same_slug(self):
        # The aborted attempt leaves notes in api; the next pick waits until they are saved.
        result, logs, context = self.walk(
            WALK_DIRTY="api", WALK_CLEAN_ON=json.dumps([["dirty working tree", "ws/api/NOTES.local"]]),
            WALK_REPLIES=json.dumps([["Do you authorize implementing", ["please stop", "approve"]]] + REPLIES[1:]))
        self.assertEqual(result.get("outcome"), "merged", context)
        [blocked] = self.sent(result, "blocked: dirty working tree")
        self.assertIn("api", blocked["body"], context)
        self.assertIn("WAIT_CLEAN", result["states"], context)
        explored = [a["branch"] for a in self.agent(logs) if a["phase"] == "EXPLORE"]
        self.assertEqual(len(set(explored)), 2, context)  # same slug, a fresh branch per attempt
        heads = {c[c.index("--head") + 1] for c in self.calls(logs, "pr", "create")}
        self.assertEqual(heads, {explored[1]}, context)
        self.assertEqual(self.merged(), ["ui", "api"], context)

    def test_dirty_hold_waits_clean_then_continue_resumes_the_task(self):
        result, logs, context = self.walk(
            WALK_DIRTY="api", WALK_CLEAN_ON=json.dumps([["dirty working tree", "ws/api/NOTES.local"]]),
            WALK_COMMANDS=json.dumps([["Do you authorize implementing", "HOLD", ""],
                                      ["dirty working tree", "CONTINUE", "approve"]]),
            WALK_REPLIES=json.dumps(REPLIES[1:]))
        self.assertEqual(result.get("outcome"), "merged", context)
        self.assertTrue(self.sent(result, "on hold"), context)
        self.assertTrue(self.sent(result, "blocked: dirty working tree"), context)
        phases = [a["phase"] for a in self.agent(logs)]
        self.assertEqual(phases, ["EXPLORE", "PROPOSE", "IMPLEMENT", "REVIEW"], context)  # resumed, not restarted
        explored = [a["branch"] for a in self.agent(logs) if a["phase"] == "EXPLORE"]
        heads = {c[c.index("--head") + 1] for c in self.calls(logs, "pr", "create")}
        self.assertEqual(heads, set(explored), context)
        self.assertEqual(self.merged(), ["ui", "api"], context)

    def test_repicking_a_merged_slug_opens_new_prs_and_archives_beside_the_first(self):
        result, logs, context = self.walk(WALK_ROUNDS="2")
        self.assertEqual(result.get("outcome"), "merged", context)
        self.assertEqual(result["done"], [ITEM, ITEM], context)
        creates = self.calls(logs, "pr", "create")
        self.assertEqual(sorted(self.repo_of(c) for c in creates), ["api", "api", "ui", "ui"], context)
        self.assertEqual(len({c[c.index("--head") + 1] for c in creates}), 2, context)  # never the merged PR
        self.assertEqual(self.merged(), ["ui", "api", "ui", "api"], context)
        archives = sorted(p.name for p in (self.root / "openspec/changes/archive").iterdir())
        self.assertEqual(len(archives), 2, f"{archives}{context}")
        self.assertTrue(all(SLUG in name for name in archives), context)

    def test_checks_with_a_cwd_outside_the_workspace_never_run(self):
        result, logs, context = self.walk(WALK_BAD_CWD="1")
        self.assertEqual(result.get("outcome"), "merged", context)
        self.assertEqual(sorted(self.tmp.glob("escaped-*")), [], context)
        self.assertIn("leaves the workspace", self.stderr, context)

    def test_a_large_diff_is_truncated_to_the_review_context_budget(self):
        result, logs, context = self.walk(WALK_LARGE="150", CODEBOT_REVIEW_CONTEXT_CHARS="1000")
        self.assertEqual(result.get("outcome"), "merged", context)
        [review] = [a for a in self.agent(logs) if a["phase"] == "REVIEW"]
        self.assertEqual(review["truncated"], ["api"], context)  # ui's small stat fits its share
        self.assertLess(review["listed_files"], 40, context)  # 150 files, a 500-character share


if __name__ == "__main__":
    unittest.main()
