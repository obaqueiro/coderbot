"""Immutable, isolated baseline worktrees for regression attribution."""
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
import re
import uuid
import json
import fnmatch
import sys
import subprocess

import operations
from checks import execute
from execution_identity import digest, environment_identity, snapshot
from selfheal_events import runtime_epoch


def dependency_snapshot(repo, inputs=None):
    repo = Path(repo)
    if not (repo / ".git").exists():
        return snapshot(repo, inputs, exclude=("openspec",))
    files = operations.run(['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard'],
                           cwd=repo, timeout=30).stdout.split('\0')
    entries = []
    for name in sorted(set(files) - {''}):
        if inputs is not None and not any(p in ("*", ".", "./") or
                fnmatch.fnmatch(name, p) or name == p or name.startswith(p.rstrip('/') + '/')
                for p in inputs):
            continue
        path = repo / name
        if path.name not in ('package.json', 'package-lock.json') and not (
                path.name.startswith('requirements') and path.suffix == '.txt'):
            continue
        if any(p in ('node_modules', '.venv', 'venv') for p in Path(name).parts):
            continue
        if not path.is_file():
            entries.append((name, 'missing'))
            continue
        if path.suffix == '.json':
            value = json.loads(path.read_text())
            value.pop('version', None)
            # Package scripts change the command, not the installed dependency graph.
            # The baseline still executes its own script under the runner timeout.
            if path.name == 'package.json':
                scripts = value.get('scripts', {})
                value['scripts'] = {name: command for name, command in scripts.items()
                                    if name in ('preinstall', 'install', 'postinstall',
                                                'prepublish', 'preprepare', 'prepare', 'postprepare')}
            if isinstance(value.get('packages', {}).get(''), dict):
                value['packages'][''].pop('version', None)
            entries.append((name, value))
        else:
            entries.append((name, path.read_text()))
    return digest(entries)


def dependency_inputs(check, repo):
    """Scope installs to the check's area; explicit cross-area inputs take priority."""
    if check.dependency_inputs is not None:
        return check.dependency_inputs
    if check.cwd not in (".", "./"):
        return [check.cwd]
    return ["*"]  # Root commands can delegate to any package; stay conservative.


def signature(error, roots=()):
    normalized = error.replace("\r\n", "\n").strip()
    for root in roots:
        normalized = normalized.replace(str(root), "<repo>")
    # Only duration and stack-frame line locations are volatile. Assertion values
    # and error messages remain exact; never strip arbitrary numbers or IDs.
    normalized = re.sub(r'(File "[^"]+", line )\d+', r'\1<line>', normalized)
    normalized = re.sub(r'/tmp/tmp[A-Za-z0-9_-]+(?=/|[).\s])', '/tmp/<fixture>', normalized)
    return normalized


def compare(feature, baseline, *, roots=()):
    if feature["status"] == "pass":
        return {"status": "pass", "preexisting": [], "regressions": []}
    if feature["status"] != "fail" or baseline["status"] not in ("pass", "fail"):
        return {"status": "indeterminate", "preexisting": [], "regressions": []}
    old = {signature(test, roots): error for test, error in baseline.get("failures", {}).items()}
    preexisting, regressions, ambiguous = [], [], []
    for test, error in feature.get("failures", {}).items():
        key = signature(test, roots)
        actual = signature(error, roots)
        expected = signature(old.get(key, ''), roots)
        if key in old and actual != expected and re.sub(r"\b[0-9a-f]{40}\b", '<sha>', actual) == re.sub(r"\b[0-9a-f]{40}\b", '<sha>', expected):
            # Random fixture commit IDs are not evidence of a code regression.
            # Do not assert pass either: preserve the ambiguity for diagnosis.
            ambiguous.append(test)
            continue
        target = preexisting if key in old and actual == expected else regressions
        target.append(test)
    result = {"status": "indeterminate" if ambiguous else "fail" if regressions else "pass" if preexisting else "indeterminate",
              "preexisting": preexisting, "regressions": regressions}
    if ambiguous:
        result.update(reason='volatile_fixture_identifiers_differ', ambiguous=ambiguous)
    return result


def baseline_result(check, repo, sha, store, *, audit_task=None):
    cache_task = "baseline:" + str(Path(repo).resolve()) + ":" + sha
    from checks import tool_versions, environment_revision, check_environment
    environment = environment_identity(check.argv, repo, env_keys=check.env_keys, environ=check_environment(), tools=tool_versions(check.argv, Path(repo) / check.cwd),
                                       key_path=store.path.parent / "identity.key")
    dependencies = dependency_inputs(check, repo)
    current_dependencies = dependency_snapshot(repo, dependencies)
    identity = digest({"sha": sha, "check": check.to_dict(), "environment": environment,
                        "dependencies": current_dependencies, "version": 2, "runtime_epoch": runtime_epoch(),
                       "repair_revision": environment_revision(store, repo, check.id)})
    saved = store.reusable_check(cache_task, identity)
    if saved and saved["data"]["result"]["status"] in ("pass", "fail"):
        return saved["data"]["result"]
    with worktree(repo, sha, store.path.parent) as path:
        # Absolute executables may be reused only with identical dependency inputs.
        separate = current_dependencies != dependency_snapshot(path, dependencies)
        prepared_python = None
        if separate:
            # Install only the immutable baseline's manifests in its disposable
            # worktree. Never mix feature installs into a different dependency graph.
            area = path / check.cwd
            commands = []
            if (area / "package-lock.json").is_file():
                commands.append(["npm", "ci"])
            elif (area / "package.json").is_file():
                return {"status": "indeterminate", "failures": {}, "reason": "baseline_lockfile_missing",
                        "detail": "Baseline requires its own dependency preparation but has no npm lockfile."}
            if (area / "requirements.txt").is_file():
                venv = area / ".venv"
                commands += [[sys.executable, "-m", "venv", str(venv)],
                             [str(venv / "bin/python"), "-m", "pip", "install", "-r", "requirements.txt"]]
                prepared_python = str(venv / "bin/python")
            if not commands:
                return {"status": "indeterminate", "failures": {}, "reason": "dependency_inputs_differ",
                        "detail": "Cross-area baseline dependencies need explicit preparation; no matching manifest in check cwd."}
            log = []
            for command in commands:
                try:
                    run = operations.run(command, cwd=area, timeout=900, exclusive=["baseline-prepare:" + str(path)])
                    log.append(run.stdout + "\n" + run.stderr)
                    if run.returncode:
                        raise RuntimeError("Baseline dependency install failed")
                except (RuntimeError, OSError, TimeoutError, subprocess.TimeoutExpired) as error:
                    report = store.path.parent / "outbox" / ("baseline-preparation-" + uuid.uuid4().hex + ".log")
                    report.parent.mkdir(parents=True, exist_ok=True)
                    report.write_text("\n".join(log) + "\n" + str(error))
                    return {"status": "infrastructure", "failures": {}, "reason": "baseline_preparation_failed",
                            "report": str(report), "detail": str(error)}
        # Git worktrees omit ignored installed dependencies. Reuse the verified
        # installations only when dependency manifests match; create links solely
        # in the disposable baseline, never edit the target checkout.
        for area in dict.fromkeys((".", "backend", "frontend", "e2e", check.cwd)):
            if separate:
                break
            if not any(p in ("*", ".", "./") or p == area or p.startswith(area + "/")
                       for p in dependencies):
                continue
            for directory in ("node_modules", ".venv", "venv"):
                source = Path(repo) / area / directory
                destination = path / area / directory
                if source.is_dir() and destination.parent.is_dir() and not destination.exists():
                    destination.symlink_to(source.resolve(), target_is_directory=True)
        argv = [str(path / Path(arg).relative_to(Path(repo).resolve()))
                if arg.startswith(str(Path(repo).resolve()) + "/") and not ".venv/" in arg
                 else arg for arg in check.argv]
        if prepared_python and Path(argv[0]).name in ("python", "python3"):
            argv[0] = prepared_python
        kwargs = {"audit_task": audit_task} if audit_task else {}
        result = execute(replace(check, argv=argv), path, store, cache_task, reuse=False, **kwargs)
        result["failures"] = {signature(test, (path, repo)): signature(error, (path, repo))
                              for test, error in result.get("failures", {}).items()}
    store.record_check(cache_task, identity, result=result)
    return result


@contextmanager
def worktree(repo, sha, data_dir):
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("Baseline must use a full immutable base SHA")
    root = Path(data_dir) / "baselines"
    root.mkdir(parents=True, exist_ok=True)
    path = root / uuid.uuid4().hex
    result = operations.run(["git", "worktree", "add", "--detach", str(path), sha], cwd=repo, timeout=120)
    if result.returncode:
        raise RuntimeError("Could not prepare baseline worktree: " + result.stderr[-500:])
    try:
        yield path
    finally:
        removed = operations.run(["git", "worktree", "remove", "--force", str(path)], cwd=repo, timeout=120)
        if removed.returncode:
            raise RuntimeError("Baseline cleanup failed: " + removed.stderr[-500:])
