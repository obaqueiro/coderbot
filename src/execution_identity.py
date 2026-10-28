"""Content and environment identities, never a git-status approximation."""
import fnmatch
import hashlib
import json
import hmac
import os
from pathlib import Path
import subprocess


EXCLUDED_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache",
                 "test-results", "playwright-report"}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def environment_identity(argv, cwd, *, env_keys=(), environ=None, tools=None, key_path):
    """Use a private local HMAC key: secret values never become stored metadata."""
    key_path = Path(key_path)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, "wb") as output:
            output.write(os.urandom(32))
    key = key_path.read_bytes()
    env = os.environ if environ is None else environ
    private = hmac.new(key, json.dumps({name: env.get(name) for name in sorted(env_keys)},
                       sort_keys=True).encode(), hashlib.sha256).hexdigest()
    return digest({"argv": list(argv), "cwd": str(Path(cwd).resolve()),
                   "environment": private, "tools": tools or {}})


def snapshot(repo, inputs=None, *, exclude=()):
    repo = Path(repo).resolve()
    if not (repo / ".git").exists():
        # Workspace roots have no Git index. Child Git roots and shared source
        # files all contribute; dependency changes invalidate integration evidence.
        return workspace_snapshot(repo, inputs, exclude=exclude)
    return digest(_without(_repository_entries(repo, inputs or ["*"]), exclude))


def _without(entries, exclude):
    return [entry for entry in entries if not any(entry[0] == p.rstrip("/") or entry[0].startswith(p.rstrip("/") + "/") for p in exclude)]


def verification_snapshot(repo, inputs=None):
    """Planning completion/archive is not a code change; scope has its own gate."""
    explicit_planning = inputs and any(p == "openspec" or p.startswith("openspec/") for p in inputs)
    return snapshot(repo, inputs, exclude=() if explicit_planning else ("openspec",))


def _repository_entries(repo, inputs, prefix=""):
    """Tracked and untracked source, respecting each repository's ignore rules."""
    files = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=repo, capture_output=True, timeout=30, check=True).stdout.decode().split("\0")
    selected = []
    patterns = ["*" if p in (".", "./") else p.removeprefix("./") for p in inputs]
    for name in sorted(set(files) - {""}):
        path = Path(name)
        if any(part in EXCLUDED_DIRS for part in path.parts):
            continue
        if path.name == ".env" or path.name.startswith(".env.") and not path.name.endswith("example"):
            continue
        if prefix and (path.suffix in {".pem", ".key", ".mp4", ".webm"} or
                       path.name in {"credentials.json", "token.json"}):
            continue  # Preserve workspace secret/media exclusions inside Git children.
        identity_name = prefix + name
        if not any(fnmatch.fnmatch(identity_name, p) or identity_name == p or identity_name.startswith(p.rstrip("/") + "/")
                   for p in patterns):
            continue
        full = repo / name
        if full.is_symlink():
            # Link identity is relevant; never follow links into external secret/caches.
            selected.append((identity_name, "link", str(full.readlink())))
        elif full.is_file():
            selected.append((identity_name, full.stat().st_mode & 0o111, hashlib.sha256(full.read_bytes()).hexdigest()))
        else:
            selected.append((identity_name, "missing"))
    return selected


def workspace_snapshot(root, inputs=None, *, exclude=()):
    from workspace import EXCLUDED_DIRS as caches
    root = Path(root).resolve()
    selected = []
    patterns = inputs or ["*"]

    def walk(directory):
        for full in sorted(directory.iterdir()):
            name = full.relative_to(root).as_posix()
            if full.name in caches or full.name in {"output", "data", ".DS_Store"}:
                continue
            if full.name == ".env" or full.name.startswith(".env.") and not full.name.endswith("example"):
                continue
            if full.is_symlink():
                selected.append((name, "link", str(full.readlink())))
            elif full.is_dir() and directory == root and (full / ".git").exists():
                # Discover only immediate Git children, including worktrees. Their
                # ignored build outputs must not invalidate workspace evidence.
                selected.extend(_repository_entries(full, patterns, prefix=name + "/"))
            elif full.is_dir():
                walk(full)
            elif full.is_file() and any(p in ("*", ".", "./") or fnmatch.fnmatch(name, p)
                    or name.startswith(p.rstrip("/") + "/") for p in patterns):
                if full.suffix in {".pem", ".key", ".mp4", ".webm"} or full.name in {"credentials.json", "token.json"}:
                    continue
                selected.append((name, full.stat().st_mode & 0o111, hashlib.sha256(full.read_bytes()).hexdigest()))
    walk(root)
    return digest(_without(selected, exclude))
