"""End-to-end walk of workspace mode through the real tick loop: a non-Git root with
children `api`, `ui` and `unused`, each with a bare origin behind a GitHub-looking URL
(git `insteadOf`). Fake `claude`/`gh` (tests/workspace_walk/bin) and the FSM walk's
channel/backlog driver (tests/fsm_walk/driver.py). Never touches the real data/."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.test_fsm_walk import _openspec_version, git

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "tests" / "workspace_walk" / "bin"
ITEM = "Greet users from the API in the UI"
SLUG = "greet-users-from-the-api-in-the-ui"


@unittest.skipUnless((_openspec_version() or (0,)) >= (1, 9, 0), "needs the pinned openspec >= 1.9.0")
class WorkspaceWalk(unittest.TestCase):
    def walk(self, **extra):
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
            self.origins[name] = origin
        if extra.get("WALK_DROP_HEAD"):  # an unrelated child with no discoverable base branch
            git(root / extra["WALK_DROP_HEAD"], "symbolic-ref", "--delete", "refs/remotes/origin/HEAD")
        (root / "workspace.json").write_text(json.dumps({"merge_order": ["ui", "api"]}))
        self.initial_unused = git(tmp, "--git-dir", str(self.origins["unused"]), "rev-parse", "main")
        env.update({"WALK_DIR": str(tmp), "WALK_ITEM": ITEM, "WALK_SLUG": SLUG,
                    "WALK_REPLIES": json.dumps([["Do you authorize implementing", "approve"],
                                                ["PRs ready for review", "merge"],
                                                ["closed without merging", "retry"]]),
                    "WALK_REPLY_EACH": "1", "WALK_ORIGIN": str(self.origins["api"]),
                    "CODEBOT_REPO_PATH": str(root), "CODEBOT_DATA_DIR": str(data),
                    "CODEBOT_WORKSPACE_MANIFEST": "workspace.json",
                    "CODEBOT_TASK_SOURCE": "gdoc", "CODEBOT_DOC_ID": "walk", "CODEBOT_COMM_CHANNEL": "email",
                    "CODEBOT_USER_EMAIL": "walk@example.com", "CODEBOT_AGENT": "claude",
                    "CODEBOT_INSTANCE": "walkbot", "CODEBOT_POLL_INTERVAL": "1",
                    "CODEBOT_EVIDENCE_UPLOAD": "off", "CODEBOT_ACTIVITY_TRAIL": "off",
                    "CODEBOT_SELF_HEAL_CODE_REVIEW": "off", "CODEBOT_LOG_LEVEL": "INFO", "GH_TOKEN": "walk",
                    "PYTHONDONTWRITEBYTECODE": "1", **extra})
        proc = subprocess.run([sys.executable, str(ROOT / "tests/fsm_walk/driver.py")], env=env, cwd=ROOT,
                              capture_output=True, text=True, timeout=600)
        result = json.loads((tmp / "walk.json").read_text()) if (tmp / "walk.json").exists() else {}
        logs = {name: (tmp / name).read_text() if (tmp / name).exists() else ""
                for name in ("claude.log", "gh.log", "gh-unsupported.log")}
        unknown = sorted({p.read_text()[:300] + " … " + p.read_text()[-300:] for p in tmp.glob("unknown-prompt-*.txt")})
        context = (f"\nfixture={tmp}\noutcome={result.get('outcome')!r} states={result.get('states')}\n"
                   f"sent={[m['subject'] for m in result.get('sent', [])]}\n"
                   f"stuck={[m['body'] for m in result.get('sent', []) if 'stuck' in m['subject']]}\n"
                   f"unknown prompts={unknown}\ngh unsupported={logs['gh-unsupported.log']}\n"
                   f"stderr tail:\n{proc.stderr[-6000:]}")
        self.root, self.tmp = root, tmp
        return result, logs, context

    def calls(self, logs):
        return [json.loads(line) for line in logs["gh.log"].splitlines()]

    def repo_of(self, call):
        return call[call.index("--repo") + 1].split("/")[-1] if "--repo" in call else None

    def test_feature_walks_across_repositories_to_ordered_merges(self):
        result, logs, context = self.walk()
        self.assertEqual(result.get("outcome"), "merged", context)
        self.assertEqual(result["done"], [ITEM], context)
        self.assertEqual(logs["gh-unsupported.log"], "", context)
        calls = self.calls(logs)
        creates = [c for c in calls if c[:2] == ["pr", "create"]]
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
        self.assertIn("Add greet() with a unit test",
                      git(self.tmp, "--git-dir", str(self.origins["api"]), "log", "--oneline", "main"), context)
        self.assertEqual(git(self.tmp, "--git-dir", str(self.origins["unused"]), "rev-parse", "main"),
                         self.initial_unused, context)
        for name in ("api", "ui", "unused"):
            self.assertEqual(git(self.root / name, "branch", "--show-current"), "main", context)
        self.assertEqual(git(self.root / "unused", "branch", "--list"), "* main", context)
        subjects = [m["subject"].split(" — ")[-1] for m in result["sent"]]
        self.assertIn("draft PR", subjects, context)
        self.assertFalse(any("stuck" in s for s in subjects), context)
        agent = [json.loads(line) for line in logs["claude.log"].splitlines()]
        working = [a for a in agent if not a["utility"]]
        self.assertEqual([a["phase"] for a in working], ["EXPLORE", "PROPOSE", "IMPLEMENT", "REVIEW"], context)
        self.assertTrue(all(a["gh_token"] == "" and a["credential_helper"] == "" for a in working), context)

    def test_closed_pr_is_reopened_after_retry_without_touching_the_other(self):
        result, logs, context = self.walk(WALK_CLOSE="ui")
        self.assertEqual(result.get("outcome"), "merged", context)
        calls = self.calls(logs)
        creates = [self.repo_of(c) for c in calls if c[:2] == ["pr", "create"]]
        self.assertEqual(sorted(creates), ["api", "ui", "ui"], context)
        self.assertTrue(any("closed without merging" in m["body"] for m in result["sent"]), context)
        self.assertEqual(sorted(self.repo_of(c) for c in calls if c[:2] == ["pr", "merge"]), ["api", "ui"], context)

    def test_a_child_losing_origin_head_does_not_stop_the_feature(self):
        result, logs, context = self.walk(WALK_DROP_HEAD="unused")
        self.assertEqual(result.get("outcome"), "merged", context)
