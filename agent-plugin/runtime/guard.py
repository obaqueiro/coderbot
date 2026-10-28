"""Claude Code PreToolUse guard for coderbot's lifecycle boundaries.

Reads the tool call on stdin and the policy from $CODEBOT_POLICY (src/tool_policy.py).
Denies coderbot-only commands even when wrapped (`bash -c "…"`, `env X=1 …`,
`cd dir && …`, `git -C dir …`, `git -c k=v …`) and, in planning phases, writes outside
the OpenSpec tree. Denial is reported through the JSON decision only. It always exits 0:
any error here lets the call through, so a broken guard can never stall a turn
(Claude Code treats exit code 2 as a block)."""
import json
import os
import re
import shlex
import sys

WRAPPERS = {"sudo", "command", "exec", "nohup", "time", "nice", "xargs"}


def commands(text, depth=0):
    """Every simple command in a shell string, as normalized word lists."""
    if depth > 4:
        return
    for part in re.split(r"&&|\|\||;|\||\n|\$\(|`|\(|\)", text):
        try:
            words = shlex.split(part)
        except ValueError:
            words = part.split()
        while words and (words[0] in WRAPPERS or re.fullmatch(r"\w+=.*", words[0]) or words[0] == "env"):
            words = words[1:]
        if len(words) >= 3 and os.path.basename(words[0]) in ("bash", "sh", "zsh", "dash") and words[1] in ("-c", "-lc", "-ec"):
            yield from commands(words[2], depth + 1)
            continue
        if words and os.path.basename(words[0]) == "git":
            rest, normalized = words[1:], ["git"]
            while rest and (rest[0] in ("-C", "-c", "--git-dir", "--work-tree") or rest[0].startswith(("--git-dir=", "--work-tree=", "-c"))):
                rest = rest[2:] if rest[0] in ("-C", "-c", "--git-dir", "--work-tree") else rest[1:]
            words = normalized + rest
        if words:
            yield [os.path.basename(words[0]), *words[1:]]


def decide(call, policy):
    tool, data = call.get("tool_name", ""), call.get("tool_input") or {}
    if tool == "Bash":
        for words in commands(str(data.get("command", ""))):
            line = " ".join(words)
            for denied in policy.get("denied", []):
                if line == denied or line.startswith(denied + " "):
                    return f"`{denied}` is run by coderbot, not by the agent; finish your work and report it"
    scope = policy.get("write_scope")
    path = data.get("file_path") or data.get("notebook_path")
    if scope and path and tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        repo = os.path.realpath(policy.get("repo", "."))
        target = os.path.realpath(os.path.join(repo, path))
        allowed = [os.path.join(repo, root) for root in scope] + [os.path.realpath(policy.get("outbox", "/nonexistent"))]
        if not any(target == root or target.startswith(root + os.sep) for root in allowed):
            return f"phase {policy.get('phase')} writes only under {', '.join(scope)}/ (OpenSpec artifacts)"
    return None


def main():
    try:
        reason = decide(json.load(sys.stdin), json.loads(os.environ.get("CODEBOT_POLICY") or "{}"))
        if reason:
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                  "permissionDecision": "deny", "permissionDecisionReason": reason}}))
    except Exception:  # noqa: BLE001 - fail open by design
        pass


if __name__ == "__main__":
    main()
    sys.exit(0)
