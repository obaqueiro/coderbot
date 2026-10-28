"""Explicit target contexts and read-only, bounded repository discovery."""
from dataclasses import dataclass
import fnmatch
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
from urllib.parse import urlsplit


EXCLUDED_DIRS = frozenset({".git", "node_modules", ".venv", "venv", "__pycache__",
                           ".pytest_cache", ".next", "dist", "build", "test-results",
                           "playwright-report"})


class WorkspaceError(ValueError):
    """An unsafe or incomplete target configuration; callers must not guess."""


def contained(root, path):
    root = Path(root).resolve()
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root):
        raise WorkspaceError(f"Path resolves outside workspace: {path}")
    return resolved


def _relative(value, label):
    if not isinstance(value, str) or not value or "\\" in value:
        raise WorkspaceError(f"{label} must be a relative POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or value == ".":
        raise WorkspaceError(f"{label} must stay beneath the workspace")
    return value


@dataclass(frozen=True)
class Manifest:
    max_depth: int = 1
    exclude: tuple[str, ...] = ()
    base_branches: tuple[tuple[str, str], ...] = ()
    shared_inputs: tuple[str, ...] = ("openspec", "docs", "scripts", "e2e",
                                    "*compose*.y*ml", "AGENTS.md", "INSTRUCTIONS.md",
                                    "requirements.md")
    # Keep optional execution settings immutable until their adapters validate them.
    checks_json: str = "[]"
    docker_json: str = "{}"

    def base_for(self, relative_path):
        return dict(self.base_branches).get(relative_path)


def load_manifest(root, path=None):
    if not path:
        return Manifest()
    source = contained(root, path)
    try:
        value = json.loads(source.read_text())
    except (OSError, ValueError) as error:
        raise WorkspaceError(f"Cannot read workspace manifest {source}: {error}") from error
    allowed = {"max_depth", "exclude", "base_branches", "shared_inputs", "checks", "docker"}
    if not isinstance(value, dict) or set(value) - allowed:
        raise WorkspaceError("Workspace manifest must be an object with supported keys")
    # Accept old manifests without allowing them to re-enable recursion.
    depth = value.get("max_depth", 1)
    if type(depth) is not int or not 1 <= depth <= 16:
        raise WorkspaceError("Workspace max_depth must be an integer between 1 and 16")
    patterns = {}
    for key in ("exclude", "shared_inputs"):
        entries = value.get(key, getattr(Manifest(), key))
        if not isinstance(entries, (list, tuple)):
            raise WorkspaceError(f"Workspace {key} must be a list")
        patterns[key] = tuple(_relative(entry, key) for entry in entries)
    bases = value.get("base_branches", {})
    if not isinstance(bases, dict):
        raise WorkspaceError("Workspace base_branches must be an object")
    for repo, branch in bases.items():
        _relative(repo, "base_branches repository")
        if (not isinstance(branch, str) or not branch or branch.startswith("-")
                or not re.fullmatch(r"[A-Za-z0-9_./-]+", branch)
                or ".." in branch or "//" in branch or branch.endswith(("/", ".", ".lock"))):
            raise WorkspaceError(f"Invalid base branch for {repo}")
    checks, docker = value.get("checks", []), value.get("docker", {})
    if not isinstance(checks, list) or not all(isinstance(check, dict) for check in checks):
        raise WorkspaceError("Workspace checks must be a list of objects")
    for check in checks:
        from check_plan import Check
        if set(check) - (set(Check.__dataclass_fields__) | {"repositories"}):
            raise WorkspaceError("Unsupported workspace check keys")
        if not isinstance(check.get("id"), str) or not check["id"]:
            raise WorkspaceError("Workspace checks require an id")
        argv = check.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) and a for a in argv):
            raise WorkspaceError("Workspace checks require a nonempty argv list")
        if check.get("cwd", ".") != ".":
            contained(root, _relative(check["cwd"], "check cwd"))
        repos = check.get("repositories", [])
        if not isinstance(repos, list):
            raise WorkspaceError("Check repositories must be a list")
        for repo in repos:
            _relative(repo, "check repository")
        if check.get("scope", "full") not in ("full", "focused"):
            raise WorkspaceError("Invalid workspace check scope")
        try:
            Check(**{k: v for k, v in check.items() if k != "repositories"})
        except (ValueError, TypeError) as error:
            raise WorkspaceError("Invalid workspace check contract") from error
    if len({check["id"] for check in checks}) != len(checks):
        raise WorkspaceError("Workspace check ids must be unique")
    if not isinstance(docker, dict) or set(docker) - {"context", "host_root", "compose_files"}:
        raise WorkspaceError("Workspace docker must be an object with supported keys")
    if "context" in docker and (not isinstance(docker["context"], str) or not docker["context"]):
        raise WorkspaceError("Docker context must be nonempty")
    if "host_root" in docker and (not isinstance(docker["host_root"], str)
                                  or not docker["host_root"].startswith("/")):
        raise WorkspaceError("Docker host_root must be an absolute POSIX path")
    files = docker.get("compose_files", [])
    if not isinstance(files, list):
        raise WorkspaceError("Docker compose_files must be a list")
    for file in files:
        contained(root, _relative(file, "compose file"))
    return Manifest(1, patterns["exclude"], tuple(sorted(bases.items())),
                    patterns["shared_inputs"], json.dumps(checks, sort_keys=True),
                    json.dumps(docker, sort_keys=True))


def git(repo, *args, required=True):
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                            text=True, timeout=60)
    if result.returncode and required:
        # Do not expose remotes with embedded credentials in exception text.
        raise WorkspaceError(f"Git {args[0]} failed in {repo}; check repository configuration")
    return result.stdout.strip() if not result.returncode else ""


def remote_identity(url):
    """Normalize transport differences without retaining URL credentials."""
    if "://" not in url:
        match = re.fullmatch(r"(?:[^@/]+@)?([^/:]+):(.+)", url)
        if match:
            host, path = match.groups()
            return host.lower() + "/" + path.removesuffix(".git").strip("/")
        return str(Path(url).resolve())
    parsed = urlsplit(url)
    if parsed.scheme == "file":
        return str(Path(parsed.path).resolve())
    if not parsed.hostname or not parsed.path.strip("/"):
        raise WorkspaceError("Repository origin must identify a host and repository")
    host = parsed.hostname.lower()
    if parsed.port:
        host += f":{parsed.port}"
    return host + "/" + parsed.path.removesuffix(".git").strip("/")


@dataclass(frozen=True)
class Repository:
    path: Path
    relative_path: str
    remote: str
    base_branch: str
    nested_roots: tuple[str, ...] = ()

    @property
    def identity(self):
        from execution_identity import digest
        return digest([self.relative_path, self.remote])

    def owns(self, relative_path):
        path = contained(self.path, relative_path)
        relative = path.relative_to(self.path).as_posix()
        return not any(relative == child or relative.startswith(child + "/")
                       for child in self.nested_roots)


@dataclass(frozen=True)
class Workspace:
    root: Path
    mode: str
    repositories: tuple[Repository, ...]
    manifest: Manifest

    @property
    def identity(self):
        from execution_identity import digest
        return digest([str(self.root), self.mode])

    @property
    def planning_root(self):
        return self.root

    def repository(self, relative_path):
        for repo in self.repositories:
            if repo.relative_path == relative_path:
                return repo
        raise WorkspaceError(f"Not a discovered repository: {relative_path}")


def _repository(root, path, manifest, base_override=None):
    if Path(git(path, "rev-parse", "--show-toplevel")).resolve() != path:
        raise WorkspaceError(f"Not an independent Git root: {path}")
    relative = path.relative_to(root).as_posix()
    url = git(path, "remote", "get-url", "origin", required=False)
    if not url:
        raise WorkspaceError(f"Repository {relative} has no origin remote")
    base = base_override or manifest.base_for(relative)
    if not base:
        default = git(path, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD", required=False)
        prefix = "refs/remotes/origin/"
        if default.startswith(prefix):
            base = default[len(prefix):]
    if not base:
        raise WorkspaceError(f"Cannot determine base branch for {relative}; configure base_branches "
                             "or set origin/HEAD (git remote set-head origin -a)")
    if not git(path, "check-ref-format", "--branch", base, required=False):
        raise WorkspaceError(f"Invalid base branch for {relative}")
    if not (git(path, "rev-parse", "--verify", f"refs/remotes/origin/{base}^{{commit}}", required=False)
            or git(path, "rev-parse", "--verify", f"refs/heads/{base}^{{commit}}", required=False)):
        raise WorkspaceError(f"Base branch {base} not available in {relative}; fetch it first")
    return Repository(path, relative, remote_identity(url), base)


def repository_paths(root, exclude=()):
    """Only immediate children are candidates; never inspect their descendants."""
    root = Path(root)
    return tuple(child for child in sorted(root.iterdir())
                 if not child.is_symlink() and child.is_dir() and child.name not in EXCLUDED_DIRS
                 and not any(fnmatch.fnmatch(child.name, pattern) for pattern in exclude)
                 and (child / ".git").exists())


def discover(root, *, mode=None, manifest_path=None, base_branch=None):
    root = Path(root).resolve()
    if mode is None:
        mode = "repo" if (root / ".git").exists() else "workspace"
    if mode not in ("repo", "workspace"):
        raise WorkspaceError("Target mode must be repo or workspace")
    if not root.is_dir():
        raise WorkspaceError(f"Target directory does not exist: {root}")
    manifest = load_manifest(root, manifest_path)
    if mode == "repo":
        return Workspace(root, mode, (_repository(root, root, manifest, base_branch or "main"),), manifest)
    if (root / ".git").exists():
        raise WorkspaceError("Workspace mode requires a non-Git root; use repo mode for a repository")
    repos = [_repository(root, path, manifest) for path in repository_paths(root, manifest.exclude)]
    return Workspace(root, mode, tuple(repos), manifest)
