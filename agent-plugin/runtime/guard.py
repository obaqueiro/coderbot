"""Claude Code PreToolUse guard for coderbot's lifecycle boundaries.

Reads the tool call on stdin and the policy from $CODEBOT_POLICY (src/tool_policy.py).
Denies coderbot-only commands even when wrapped (`bash -c "…"`, `env X=1 …`,
`cd dir && …`, `git -C dir …`, `git -c k=v …`, `GIT_DIR=… git …`) and, in planning
phases, writes outside the OpenSpec tree. In workspace mode it also denies `git init` at
the root, mutating `gh api` calls and writes to protected controller files. Denial is
reported through the JSON decision only. It always exits 0: any error here lets the call
through, so a broken guard can never stall a turn (Claude Code treats exit 2 as a block)."""
import json
import os
import re
import shlex
import sys

WRAPPERS = {"sudo", "command", "exec", "nohup", "time", "nice", "xargs"}
ASSIGNMENT = re.compile(r"(\w+)=(.*)", re.DOTALL)
GIT_VALUE_OPTIONS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}
GIT_FLAGS = {"-P", "--no-pager", "--paginate", "-p", "--bare", "--no-replace-objects",
             "--literal-pathspecs", "--no-optional-locks"}
WRITERS = {"tee", "cp", "mv", "rm", "truncate", "dd", "install", "ln", "touch"}


def _split(part):
    try:
        return shlex.split(part)
    except ValueError:
        return part.split()


def _git_dir_location(value, here):
    path = os.path.join(here, value)
    return os.path.dirname(path) if os.path.basename(os.path.normpath(path)) == ".git" else path


def commands(text, cwd=".", depth=0):
    """Every simple command in a shell string: (normalized words, effective directory)."""
    if depth > 4:
        return
    here = cwd
    for part in re.split(r"&&|\|\||;|\||\n|\$\(|`|\(|\)", text):
        words, location = _split(part), here
        while words and (words[0] in WRAPPERS or ASSIGNMENT.fullmatch(words[0]) or words[0] == "env"):
            match = ASSIGNMENT.fullmatch(words[0])
            if match and match[1] in ("GIT_DIR", "GIT_WORK_TREE"):
                location = _git_dir_location(match[2], here)
            words = words[1:]
        if not words:
            continue
        if words[0] == "cd":
            here = os.path.join(here, words[1]) if len(words) > 1 and not words[1].startswith("-") else here
            continue
        if len(words) >= 3 and os.path.basename(words[0]) in ("bash", "sh", "zsh", "dash") and words[1] in ("-c", "-lc", "-ec"):
            yield from commands(words[2], here, depth + 1)
            continue
        if os.path.basename(words[0]) == "git":
            rest = words[1:]
            while rest and (rest[0] in GIT_VALUE_OPTIONS | GIT_FLAGS or rest[0].startswith(("--git-dir=", "--work-tree=", "-c"))):
                option, value = rest[0], rest[1] if len(rest) > 1 else ""
                if option.startswith(("--git-dir=", "--work-tree=")):
                    option, value = option.split("=", 1)
                    rest = rest[1:]
                elif option in GIT_VALUE_OPTIONS:
                    rest = rest[2:]
                else:
                    rest = rest[1:]
                    continue
                if option == "-C":
                    location = os.path.join(location, value)
                elif option in ("--git-dir", "--work-tree"):
                    location = _git_dir_location(value, location)
            words = ["git", *rest]
        yield [os.path.basename(words[0]), *words[1:]], location


def _redirect_targets(text):
    """Files a shell string writes through `>`/`>>` redirections."""
    return [match[1] for match in re.finditer(r"(?<![0-9&])>>?\s*([^\s;&|<>()]+)", text)]


def _mutating_gh_api(words):
    if words[:2] != ["gh", "api"]:
        return False
    args = words[2:]
    method = next((args[i + 1] for i, a in enumerate(args[:-1]) if a in ("-X", "--method")), None)
    method = method or next((a.split("=", 1)[1] for a in args if a.startswith("--method=")), None)
    method = method or next((a[2:] for a in args if a.startswith("-X") and len(a) > 2), None)
    if method:
        return method.upper() not in ("GET", "HEAD")
    if "graphql" in args:
        return any(re.search(r"\bmutation\b", a) for a in args)
    return any(a in ("-f", "-F", "--field", "--raw-field", "--input") or a.startswith(("--field=", "--raw-field=", "--input="))
               for a in args)


def _protected(path, policy):
    target = os.path.realpath(path)
    return any(target == os.path.realpath(p) for p in policy.get("protected") or []) or (
        policy.get("workspace") and target.endswith(os.sep + os.path.join(".codebot", "checks.json")))


def _bash(command, policy, cwd):
    root = os.path.realpath(policy.get("repo", "."))
    for words, location in commands(command, cwd):
        line = " ".join(words)
        for denied in policy.get("denied", []):
            if line == denied or line.startswith(denied + " "):
                return f"`{denied}` is run by coderbot, not by the agent; finish your work and report it"
        if not policy.get("workspace"):
            continue
        if words[:2] == ["git", "init"]:
            target = next((w for w in words[2:] if not w.startswith("-")), ".")
            if os.path.realpath(os.path.join(location, target)) == root:
                return "the workspace root is not a Git repository; run Git only inside a child repository"
        if _mutating_gh_api(words):
            return "GitHub writes (PRs, comments, reviews) are made by coderbot; report what to change instead"
        writer = words[0] in WRITERS or words[0] in ("sed", "perl") and any(w.startswith("-i") for w in words[1:])
        paths = [w for w in words[1:] if not w.startswith("-")] if writer else []
        if any(_protected(os.path.join(location, p), policy) for p in paths):
            return "the workspace manifest and .codebot/checks.json are controller configuration; do not modify them"
    if policy.get("workspace") and any(_protected(os.path.join(cwd, p), policy) for p in _redirect_targets(command)):
        return "the workspace manifest and .codebot/checks.json are controller configuration; do not modify them"
    return None


def decide(call, policy):
    tool, data = call.get("tool_name", ""), call.get("tool_input") or {}
    repo = os.path.realpath(policy.get("repo", "."))
    if tool == "Bash":
        reason = _bash(str(data.get("command", "")), policy, call.get("cwd") or repo)
        if reason:
            return reason
    path = data.get("file_path") or data.get("notebook_path")
    if not path or tool not in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        return None
    target = os.path.realpath(os.path.join(repo, path))
    if _protected(target, policy):
        return "the workspace manifest and .codebot/checks.json are controller configuration; do not modify them"
    scope = policy.get("write_scope")
    if scope:
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
