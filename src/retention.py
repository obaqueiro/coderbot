"""Bounded disk use and a startup runtime probe; both best-effort, neither blocks."""
import logging
import re
import shutil
import time
from pathlib import Path

import operations

log = logging.getLogger(__name__)

KEEP_DAYS = 14
_DAY = 86400
# Swept children of DATA_DIR: per-run check reports, incident diagnostics, recovery
# worktrees. Anything newer than KEEP_DAYS, or still referenced, stays.
SWEPT = ("outbox/checks", "diagnostics", "workspaces")
# The CLI versions the image pins (Dockerfile) and the flags coderbot passes them.
PINNED = {"claude": "2.1.257", "opencode": "1.18.18", "openspec": "1.9.0"}
REQUIRED_FLAGS = {"claude": ("--resume", "--model", "--effort", "--plugin-dir", "--output-format",
                             "--dangerously-skip-permissions", "--tools", "--strict-mcp-config",
                             "--mcp-config", "--setting-sources", "--disable-slash-commands",
                             "--append-system-prompt")}


def due(stamp: Path, now=None) -> bool:
    try:
        return (now or time.time()) - stamp.stat().st_mtime >= _DAY
    except FileNotFoundError:
        return True


def sweep(data_dir: Path, repo: Path, keep=(), store=None, now=None) -> int:
    """Remove swept entries older than KEEP_DAYS (except paths in `keep`), prune stale
    git worktrees and old finished checkpoints. Returns how many entries went."""
    now = now or time.time()
    cutoff, kept, removed = now - KEEP_DAYS * _DAY, {Path(p).resolve() for p in keep if p}, 0
    for relative in SWEPT:
        root = Path(data_dir) / relative
        for entry in (root.iterdir() if root.is_dir() else ()):
            try:
                if entry.resolve() in kept or entry.lstat().st_mtime >= cutoff:
                    continue
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    entry.unlink()
                removed += 1
            except OSError as error:
                log.debug("retention: could not remove %s: %s", entry, error)
    for temp in Path(data_dir).glob(".*.tmp"):  # left by an atomic write killed mid-way
        try:
            if temp.stat().st_mtime < now - _DAY:
                temp.unlink()
        except OSError:
            pass
    try:
        operations.tool(["git", "worktree", "prune"], cwd=repo, timeout=60)
    except Exception as error:  # noqa: BLE001 — missing git, timeout: retried tomorrow
        log.debug("retention: git worktree prune failed: %s", error)
    if store is not None:
        try:
            with store.connection() as db:
                removed += db.execute("DELETE FROM checkpoint WHERE status IN ('complete','consumed') "
                                      "AND updated < ?", (cutoff,)).rowcount
        except Exception as error:  # noqa: BLE001 — an old schema or a locked db just waits a day
            log.debug("retention: checkpoint cleanup failed: %s", error)
    return removed


def maybe_sweep(data_dir: Path, repo: Path, keep=(), store=None) -> None:
    """At most once a day; never raises."""
    stamp = Path(data_dir) / ".retention-stamp"
    if not due(stamp):
        return
    try:
        stamp.touch()
        removed = sweep(data_dir, repo, keep() if callable(keep) else keep, store)
        if removed:
            log.info("retention: removed %d entr%s older than %d days", removed,
                     "y" if removed == 1 else "ies", KEEP_DAYS)
    except Exception:  # noqa: BLE001 — housekeeping must never stall the loop
        log.warning("retention sweep failed", exc_info=True)


def probe(agent: str) -> list[str]:
    """Mismatches between the installed CLIs and the pinned versions/flags."""
    problems = []
    for name, pinned in PINNED.items():
        if name in ("claude", "opencode") and name != agent:
            continue
        try:
            version = operations.tool([name, "--version"], timeout=30).stdout
        except Exception as error:  # noqa: BLE001
            problems.append(f"{name}: not runnable ({error})")
            continue
        found = re.search(r"\d+\.\d+\.\d+", version)
        if not found or found.group() != pinned:
            problems.append(f"{name}: version {found.group() if found else version.strip()[:40]!r}, "
                            f"pinned {pinned}")
    for name, flags in REQUIRED_FLAGS.items():
        if name != agent:
            continue
        try:
            text = operations.tool([name, "--help"], timeout=30).stdout
        except Exception:  # noqa: BLE001 — already reported as not runnable
            continue
        missing = [flag for flag in flags if not re.search(re.escape(flag) + r"\b", text)]
        if missing:
            problems.append(f"{name}: missing flags {', '.join(missing)}")
    return problems
