"""Lifecycle guardrails live in the runtime, not in prompt prose (live check C4/C5)."""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import config
import tool_policy

GUARD = Path(__file__).resolve().parent.parent / "agent-plugin" / "runtime" / "guard.py"


def guard(call, policy):
    env = {**os.environ, "CODEBOT_POLICY": policy if isinstance(policy, str) else json.dumps(policy)}
    proc = subprocess.run([sys.executable, str(GUARD)], input=json.dumps(call) if isinstance(call, dict) else call,
                          capture_output=True, text=True, env=env, timeout=10)
    return proc.returncode, (json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"]
                             if proc.stdout.strip() else "allow")


def bash(command):
    return {"tool_name": "Bash", "tool_input": {"command": command}}


class Guard(unittest.TestCase):
    policy = {"phase": "IMPLEMENTING", "repo": "/repo", "outbox": "/data/outbox",
              "denied": list(tool_policy.denied("IMPLEMENTING")), "write_scope": None}

    def test_wrapped_forms_of_coderbot_only_commands_are_denied(self):
        for command in ("git push origin feature", "git -C . push origin feature",
                        'bash -c "git push origin HEAD"', "cd src && git push", "env GIT_TRACE=1 git push",
                        "git -c core.x=1 push", "sh -c 'gh pr merge 3 --squash'", "gh pr create --draft",
                        "make test; openspec archive change -y", "git worktree add ../x"):
            with self.subTest(command=command):
                self.assertEqual(guard(bash(command), self.policy), (0, "deny"))

    def test_lookalikes_and_ordinary_work_are_allowed(self):
        for command in ("git status", "git commit -m 'push the button'", "echo git push",
                        "grep -r 'gh pr merge' docs", "python3 -m unittest", "git log --grep push"):
            with self.subTest(command=command):
                self.assertEqual(guard(bash(command), self.policy), (0, "allow"))

    def test_planning_phases_write_only_openspec_and_never_commit(self):
        planning = {**self.policy, "phase": "PROPOSING", "denied": list(tool_policy.denied("PROPOSING")),
                    "write_scope": ["openspec"]}
        write = lambda path: {"tool_name": "Write", "tool_input": {"file_path": path}}
        self.assertEqual(guard(write("/repo/openspec/changes/x/design.md"), planning), (0, "allow"))
        self.assertEqual(guard(write("openspec/changes/x/tasks.md"), planning), (0, "allow"))
        self.assertEqual(guard(write("/data/outbox/shot.png"), planning), (0, "allow"))
        self.assertEqual(guard(write("/repo/src/app.py"), planning), (0, "deny"))
        self.assertEqual(guard(write("/repo/openspec/../src/app.py"), planning), (0, "deny"))
        self.assertEqual(guard(bash("git commit -am wip"), planning), (0, "deny"))
        self.assertEqual(guard(write("/repo/src/app.py"), self.policy), (0, "allow"))

    def test_any_error_fails_open_with_exit_zero(self):
        # Claude Code blocks a tool call on exit code 2; the guard must never do that by accident.
        for call, policy in (("not json", self.policy), (bash("git push"), "{broken"), ({}, {})):
            with self.subTest(call=call, policy=policy):
                self.assertEqual(guard(call, policy), (0, "allow"))


class Runtime(unittest.TestCase):
    def test_claude_working_turns_get_deny_rules_guard_and_no_subagents(self):
        args = tool_policy.claude_args("IMPLEMENTING")
        settings = json.loads(args[args.index("--settings") + 1])
        self.assertIn("Bash(git push:*)", settings["permissions"]["deny"])
        hook = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        self.assertIn("guard.py", hook)
        self.assertTrue(hook.endswith("|| true"))
        self.assertEqual(args[args.index("--disallowedTools") + 1:], ["Task", "Agent"])
        self.assertIn("Write", tool_policy.claude_args("EXPLORING"))
        self.assertNotIn("--disallowedTools", tool_policy.claude_args("INTERNAL_REVIEW"))
        with patch.object(config, "EXECUTION", "subagent"):
            self.assertNotIn("--disallowedTools", tool_policy.claude_args("IMPLEMENTING"))
        with patch.object(config, "TOOL_POLICY", False):
            self.assertEqual(tool_policy.claude_args("IMPLEMENTING"), [])
            self.assertEqual(tool_policy.environment("IMPLEMENTING"), {})

    def test_environment_carries_policy_and_subagent_caps(self):
        env = tool_policy.environment("PROPOSING")
        policy = json.loads(env["CODEBOT_POLICY"])
        self.assertEqual(policy["write_scope"], ["openspec"])
        self.assertIn("git commit", policy["denied"])
        self.assertEqual(env["CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH"], "1")

    def test_opencode_bash_rules_deny_after_allow_all(self):
        rules = tool_policy.opencode_bash("IMPLEMENTING")
        self.assertEqual(next(iter(rules)), "*")
        self.assertEqual(rules["git push*"], "deny")
        self.assertEqual(rules["git -C * push*"], "deny")
        self.assertEqual(rules["gh pr merge*"], "deny")


if __name__ == "__main__":
    unittest.main()
