"""End-to-end walk of the real tick loop: IDLE → explore → propose → approval →
implement → verify → review → archive → PR → merge → IDLE.

Real git (with a bare origin), real `openspec`, real controller checks; fake `claude`
and `gh` executables (tests/fsm_walk/bin) and fake channel/backlog backends
(tests/fsm_walk/driver.py). It runs coderbot in a subprocess so module-level paths
resolve against the fixture, and never touches the real data/ directory."""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fsm_walk"
ITEM = "Add a greeting function"
SLUG = "add-a-greeting-function"
HOOK = ROOT / "tests" / "workspace_walk" / "hooks" / "post-receive"
# The N=1 golden of the lifecycle seam (W9): regenerate with WALK_RECORD=1 and explain the diff.
GOLDEN = ROOT / "tests" / "fixtures" / "fsm_walk_golden.json"


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _openspec_version():
    try:
        out = subprocess.run(["openspec", "--version"], capture_output=True, text=True, timeout=30).stdout
        return tuple(int(part) for part in out.strip().split(".")[:3])
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


# The controller relies on flags of the pinned OpenSpec (Dockerfile: 1.9.0), e.g.
# `validate --archived`; scripts/test.sh runs this walk on the pinned toolchain.
@unittest.skipUnless((_openspec_version() or (0,)) >= (1, 9, 0), "needs the pinned openspec >= 1.9.0")
class FsmWalk(unittest.TestCase):
    def walk(self):
        # WALK_KEEP=1 keeps the fixture (repo, data, logs) for debugging a failed walk.
        tmp = Path(tempfile.mkdtemp(prefix="fsm-walk-") if os.environ.get("WALK_KEEP")
                   else self.enterContext(tempfile.TemporaryDirectory()))
        origin, repo, data, home = tmp / "origin.git", tmp / "repo", tmp / "data", tmp / "home"
        for path in (data, home):
            path.mkdir()
        git(tmp, "init", "-q", "--bare", "-b", "main", str(origin))
        shutil.copy2(HOOK, origin / "hooks" / "post-receive")  # logs every push to pushes.log
        git(tmp, "clone", "-q", str(origin), str(repo))
        for name, value in (("user.name", "walk"), ("user.email", "walk@example.com")):
            git(repo, "config", name, value)
        (repo / "openspec/specs").mkdir(parents=True)
        (repo / "openspec/changes").mkdir()
        (repo / "openspec/specs/.keep").write_text("")
        (repo / "openspec/changes/.keep").write_text("")
        (repo / "README.md").write_text("# walk fixture\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "initial")
        git(repo, "push", "-q", "origin", "main")
        env = {"PATH": f"{FIXTURE / 'bin'}{os.pathsep}{os.environ['PATH']}", "HOME": str(home),
               "WALK_DIR": str(tmp), "WALK_ORIGIN": str(origin), "WALK_ITEM": ITEM, "WALK_SLUG": SLUG,
               "CODEBOT_REPO_PATH": str(repo), "CODEBOT_DATA_DIR": str(data),
               "CODEBOT_TASK_SOURCE": "gdoc", "CODEBOT_DOC_ID": "walk", "CODEBOT_COMM_CHANNEL": "email",
               "CODEBOT_USER_EMAIL": "walk@example.com", "CODEBOT_AGENT": "claude",
               "CODEBOT_INSTANCE": "walkbot", "CODEBOT_BASE_BRANCH": "main", "CODEBOT_POLL_INTERVAL": "1",
               "CODEBOT_EVIDENCE_UPLOAD": "off", "CODEBOT_ACTIVITY_TRAIL": "off",
               "CODEBOT_SELF_HEAL_CODE_REVIEW": "off", "CODEBOT_LOG_LEVEL": "INFO", "GH_TOKEN": "walk",
               "GIT_AUTHOR_NAME": "walk", "GIT_AUTHOR_EMAIL": "walk@example.com",
               "GIT_COMMITTER_NAME": "walk", "GIT_COMMITTER_EMAIL": "walk@example.com",
               "PYTHONDONTWRITEBYTECODE": "1"}
        proc = subprocess.run([sys.executable, str(FIXTURE / "driver.py")], env=env, cwd=ROOT,
                              capture_output=True, text=True, timeout=600)
        result = json.loads((tmp / "walk.json").read_text()) if (tmp / "walk.json").exists() else {}
        logs = {name: (tmp / name).read_text() if (tmp / name).exists() else ""
                for name in ("claude.log", "gh.log", "gh-unsupported.log", "pushes.log")}
        (tmp / "controller.log").write_text(proc.stderr)
        unknown = sorted({p.read_text()[-700:] for p in tmp.glob("unknown-prompt-*.txt")})
        context = (f"\nfixture={tmp}\noutcome={result.get('outcome')!r} states={result.get('states')}\n"
                   f"sent={[m['subject'] for m in result.get('sent', [])]}\n"
                   f"stuck={[m['body'] for m in result.get('sent', []) if 'stuck' in m['subject']]}\n"
                   f"unknown prompts={unknown}\ngh unsupported={logs['gh-unsupported.log']}\n"
                   f"stderr tail:\n{proc.stderr[-6000:]}")
        return result, logs, git(tmp, "--git-dir", str(origin), "log", "--oneline", "main"), context

    def test_task_walks_from_backlog_to_merge(self):
        result, logs, merged, context = self.walk()
        self.assertEqual(result.get("outcome"), "merged", context)
        self.assertEqual(result["done"], [ITEM], context)
        self.assertIn("Add greet() with a unit test", merged, context)
        self.assertEqual(logs["gh-unsupported.log"], "", context)
        calls = [json.loads(line) for line in logs["gh.log"].splitlines()]
        creates = [c for c in calls if c[:2] == ["pr", "create"]]
        self.assertEqual(len(creates), 1, context)  # one PR for the whole task
        self.assertIn("--draft", creates[0], context)  # opened right after approval
        ready = next(i for i, c in enumerate(calls) if c[:2] == ["pr", "ready"])
        merge = next(i for i, c in enumerate(calls) if c[:2] == ["pr", "merge"])
        self.assertLess(calls.index(creates[0]), ready, context)
        self.assertLess(ready, merge, context)
        self.assertIn("draft PR", [m["subject"].split(" — ")[-1] for m in result["sent"]], context)

    def test_walk_matches_the_n1_golden(self):
        result, logs, _, context = self.walk()
        self.assertEqual(result.get("outcome"), "merged", context)
        observed = golden(result, logs)
        if os.environ.get("WALK_RECORD"):
            GOLDEN.write_text(json.dumps(observed, indent=1) + "\n")
        self.assertEqual(observed, json.loads(GOLDEN.read_text()), context)


def golden(result, logs):
    """The walk's side effects without run-specific values: gh calls (bodies dropped,
    SHAs, dates and GraphQL documents named), pushes the origin received, message
    subjects and the visited states."""
    def arg(value):
        if value.startswith("query="):
            return "query=<" + ("resolveReviewThread" if "resolveReviewThread" in value else "reviewThreads") + ">"
        return re.sub(r"\b[0-9a-f]{40}\b", "<sha>", re.sub(r"\d{4}-\d{2}-\d{2}", "<date>", value))
    calls = [[("<body>" if i and argv[i - 1] == "--body" else arg(a)) for i, a in enumerate(argv)]
             for argv in map(json.loads, logs["gh.log"].splitlines())]
    zero = "0" * 40
    pushes = [{"ref": p["ref"], "op": "create" if p["old"] == zero else "delete" if p["new"] == zero else "update"}
              for p in map(json.loads, logs["pushes.log"].splitlines())]
    return {"gh": calls, "pushes": pushes, "sent": [m["subject"].split(" — ")[-1] for m in result["sent"]],
            "states": result["states"]}
