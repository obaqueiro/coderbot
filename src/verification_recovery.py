"""Explicit, backed-up OpenSpec recovery; never reset application work or Git."""
import json
import re
import uuid
from pathlib import Path

import operations
import proposal_package
import task_phases


def guidance(note):
    if not note.startswith("--restore-plan"):
        return None, note
    match = re.fullmatch(r"--restore-plan\s+([0-9a-fA-F]{7,40})(?:\s*:\s*|\s+|$)(.*)", note, re.DOTALL)
    if not match:
        raise ValueError("Use VERIFY --restore-plan <commit SHA>: optional guidance")
    return match[1], match[2].strip()


def restore(repo, slug, commit, data_dir):
    """Restore whitelisted artifacts only from an ancestor, with a complete backup."""
    if not re.fullmatch(r"[0-9a-fA-F]{7,40}", commit) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
        raise ValueError("Invalid recovery commit or change slug")
    repo = Path(repo).resolve()
    root = repo / "openspec/changes" / slug
    relative = root.relative_to(repo).as_posix()

    def git(*args):
        result = operations.tool(["git", *args], cwd=repo, timeout=30, text=False)
        if result.returncode:
            raise ValueError("Cannot restore plan: " + result.stderr.decode(errors="replace").strip())
        return result.stdout

    sha = git("rev-parse", "--verify", commit + "^{commit}").decode().strip()
    git("merge-base", "--is-ancestor", sha, "HEAD")
    current = proposal_package.collect(repo, slug)
    names = git("ls-tree", "-r", "--name-only", sha, "--", relative).decode().splitlines()
    artifacts = {name[len(relative) + 1:]: git("show", sha + ":" + name)
                 for name in names if name in [relative + "/" + file for file in ("proposal.md", "design.md", "tasks.md")]
                 or re.fullmatch(re.escape(relative) + r"/specs/[^/]+/spec\.md", name)}
    # A recovery must not silently remove new requirements or introduce files.
    if set(artifacts) != set(current):
        raise ValueError("Cannot restore plan: artifact inventory differs; reconcile it explicitly")
    for entry in git("ls-tree", "-r", sha, "--", relative).decode().splitlines():
        metadata, name = entry.split("\t", 1)
        if name[len(relative) + 1:] in artifacts and metadata.split()[0] not in ("100644", "100755"):
            raise ValueError("Cannot restore nonregular OpenSpec artifacts")
    pending = list(task_phases.TASK.finditer(artifacts["tasks.md"].decode("utf-8")))
    if not pending or any(task[2] == " " for task in pending):
        raise ValueError("Cannot restore plan: selected commit does not have a completed checklist")
    for name in artifacts:
        path = root / name
        if any(parent.is_symlink() for parent in (path, *path.parents)):
            raise ValueError("Cannot restore plan through symbolic links")
    backup = Path(data_dir).resolve() / "verification-recovery" / uuid.uuid4().hex
    if backup.is_relative_to(repo):
        raise ValueError("Plan recovery backup must be outside the target repository")
    backup.mkdir(parents=True)
    for name in current:
        destination = backup / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((root / name).read_bytes())
    # Capture staged content separately without modifying it.
    (backup / "index.patch").write_bytes(git("diff", "--cached", "--binary", "--", relative))
    (backup / "manifest.json").write_text(json.dumps({"commit": sha, "slug": slug,
        "files": sorted(artifacts)}, indent=2))
    for name, content in artifacts.items():
        (root / name).write_bytes(content)
    return {"commit": sha, "backup": str(backup), "files": sorted(artifacts)}
