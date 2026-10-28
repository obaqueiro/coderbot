"""Versioned, declarative checks for the trusted deterministic runner."""
from dataclasses import asdict, dataclass, field, replace
import json
import logging
import re
import sys
import math
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class Check:
    id: str
    argv: list[str]
    cwd: str = "."
    inputs: list[str] = field(default_factory=lambda: ["*"])
    env_keys: list[str] = field(default_factory=list)
    timeout: float = 900
    resources: list[str] = field(default_factory=list)
    reporter: str = "text"
    scope: str = "full"
    preparation: dict | None = None
    dependency_inputs: list[str] | None = None
    # Optional checks (e.g. live calls to a third-party API) are skipped, not failed,
    # when any of their env_keys has no value.
    optional: bool = False
    kind: str = "auto"

    def __post_init__(self):
        self.optional = self.optional is True  # agent JSON may send "no"/"false" strings
        if self.kind not in ("auto", "unit", "functional", "e2e"):
            raise ValueError("kind must be auto, unit, functional or e2e")
        if not isinstance(self.id, str) or not self.id:
            raise ValueError("id must be a nonempty string")
        if not isinstance(self.argv, list) or not self.argv or not all(isinstance(x, str) and x for x in self.argv):
            raise ValueError("argv must be a nonempty array of nonempty strings; shell command strings are not accepted")
        if not isinstance(self.cwd, str) or not self.cwd:
            raise ValueError("cwd must be a nonempty path string")
        if (not isinstance(self.timeout, (int, float)) or isinstance(self.timeout, bool)
                or not math.isfinite(self.timeout) or self.timeout <= 0):
            raise ValueError("timeout must be a finite positive number of seconds")
        if self.scope not in ("full", "focused"):
            raise ValueError("scope must be full or focused")
        for name in ("inputs", "env_keys", "resources"):
            values = getattr(self, name)
            if not isinstance(values, list) or not all(isinstance(x, str) for x in values):
                raise ValueError(name + " must be an array of strings")
        if self.dependency_inputs is not None and (not isinstance(self.dependency_inputs, list) or
                not all(isinstance(p, str) and p for p in self.dependency_inputs)):
            raise ValueError("Check dependency inputs must be nonempty strings")

    def to_dict(self):
        value = asdict(self)
        if not value["optional"]:
            # Omitted when false so state written by this code still loads in older code.
            value.pop("optional")
        if value["kind"] == "auto":
            value.pop("kind")
        return value

    def missing_credentials(self, environ) -> list[str]:
        return [name for name in self.env_keys if not (environ.get(name) or "").strip()]


def relax(checks, names):
    """Checks needing any credential the user chose to go without become optional."""
    names = set(names or ())
    return [replace(c, optional=True) if names & set(c.env_keys) else c for c in checks]


def parse(value):
    if (not isinstance(value, dict) or type(value.get("version")) is not int or value["version"] != 1
            or not isinstance(value.get("checks"), list)):
        raise ValueError("Check plan must have version 1 and checks array")
    checks, seen = [], set()
    for index, entry in enumerate(value["checks"]):
        if not isinstance(entry, dict):
            raise ValueError(f"checks[{index}] must be a check object")
        label = entry.get("id") if isinstance(entry.get("id"), str) and entry["id"] else f"checks[{index}]"
        entry = dict(entry)  # Canonicalize metadata only; never guess executable commands.
        try:
            if "timeout_seconds" in entry:
                alias = entry.pop("timeout_seconds")
                if "timeout" in entry and (entry["timeout"] != alias or isinstance(alias, bool)):
                    raise ValueError("conflicting timeout and timeout_seconds; use only timeout")
                entry.setdefault("timeout", alias)
            unknown = set(entry) - set(Check.__dataclass_fields__)
            if unknown:
                raise ValueError("unsupported fields: " + ", ".join(sorted(unknown))
                                 + "; use canonical names such as id, argv, cwd and timeout")
            check = Check(**entry)
        except (ValueError, TypeError) as error:
            raise ValueError(f"check {label}: {error}") from error
        if check.id in seen:
            raise ValueError(f"Duplicate check id {check.id}; use unique IDs")
        seen.add(check.id)
        checks.append(check)
    return checks


def load(repo):
    path = Path(repo) / ".codebot/checks.json"
    return parse(json.loads(path.read_text())) if path.exists() else None


def execution_key(check, repo):
    value = check.to_dict()
    for key in ("id", "timeout", "resources"):
        value.pop(key, None)
    value["cwd"] = str((Path(repo) / check.cwd).resolve())
    # Only canonicalize plain invocations of an explicitly POSIX-shell script;
    # shell flags, different selectors and non-POSIX scripts retain their identity.
    argv = list(check.argv)
    index = 1 if Path(argv[0]).name in {"sh", "bash"} and len(argv) > 1 else 0
    script = Path(value["cwd"]) / argv[index]
    try:
        if not argv[index].startswith("-") and script.is_file():
            with script.open("rb") as stream:
                posix = stream.readline(128).strip() == b"#!/bin/sh"
            if posix:
                value["argv"] = ["posix-script", str(script.resolve()), *argv[index + 1:]]
    except OSError:
        pass
    return json.dumps(value, sort_keys=True)


def is_shell_syntax(check):
    """Recognize noninteractive no-exec options, not arguments passed to a script."""
    argv = check.argv
    if Path(argv[0]).name == "env":
        argv = argv[1:]
        while argv and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", argv[0]):
            argv = argv[1:]
        if argv and argv[0] == "--":
            argv = argv[1:]
    if not argv or Path(argv[0]).name not in {"bash", "sh"}:
        return False
    noexec = False
    for option in argv[1:]:
        if option in {"--noprofile", "--norc", "--posix"}:
            continue
        if option == "--":
            break
        if option.startswith("+"):
            return False  # +n can re-enable execution.
        if not re.fullmatch(r"-[A-Za-z]+", option):
            if option.startswith("-"):
                return False  # Unknown interpreter options cannot prove no-exec.
            break
        if any(flag in option[1:] for flag in "ioO"):
            return False  # Interactive shells ignore -n; named options need parsing.
        noexec |= "n" in option[1:]
        if "c" in option[1:]:
            break  # Following command/arguments are not interpreter options.
    return noexec


def is_harness(check, repo):
    if is_shell_syntax(check):
        return False
    cwd = (Path(repo) / check.cwd).resolve()
    return any((cwd / arg).resolve().name == "run.sh" and
               (cwd / arg).resolve().parent.name == "e2e" for arg in check.argv if not arg.startswith("-"))


def is_e2e(check, repo):
    if is_shell_syntax(check):
        return False
    # Known browser/harness commands cannot be relabeled as cheap unit checks.
    if (check.kind == "e2e" or is_harness(check, repo) or check.reporter == "playwright"
            or any(Path(arg).name in {"playwright", "e2e", "e2e-tests", "browser-tests"} for arg in check.argv)):
        return True
    cwd = Path(repo) / check.cwd
    text = " ".join(check.argv)
    if "npm" in check.argv:
        try:
            scripts = json.loads((cwd / "package.json").read_text()).get("scripts", {})
            text += " " + " ".join(scripts.get(name, "") for name in check.argv)
        except (OSError, ValueError):
            pass
    for arg in check.argv:
        path = cwd / arg
        if path.suffix == ".sh" and path.is_file():
            with path.open("rb") as stream:
                text += " " + stream.read(65536).decode(errors="replace")
    return bool(re.search(r"\bplaywright\s+test\b|(?:^|/)e2e/run\.sh\b", text))


def has_e2e_selection(check):
    for i, arg in enumerate(check.argv):
        option, equal, inline = arg.partition("=")
        if equal and option in {"--grep", "--test-filter"} and inline not in {"*", ".*", ".+", "^", "$", ""}:
            return True
        if arg in {"--grep", "-g", "--test-filter", "-k"} and i + 1 < len(check.argv):
            if check.argv[i + 1] not in {"*", ".*", ".+", "^", "$", ""}:
                return True
        if any(part in Path(arg).name for part in (".spec.", ".test.", "test_")) and not any(c in arg for c in "*?"):
            return True
    return False


def feature_checks(plan, repo):
    """A registered broad suite is not a required feature check; real selections are."""
    required = []
    for check in plan:
        if not is_e2e(check, repo):
            required.append(check)
        elif check.scope == "focused" or has_e2e_selection(check):
            required.append(replace(check, scope="focused"))
    return required


def normalize(checks, repo):
    """Collapse execution aliases, not distinct selections or test contracts."""
    unique = {}
    for check in checks:
        key = execution_key(check, repo)
        previous = unique.get(key)
        resources = set(check.resources) | (set(previous.resources) if previous else set())
        timeout = max(check.timeout, previous.timeout if previous else 0)
        if is_e2e(check, repo):
            resources.add("harness:" + str(Path(repo).resolve()))
            timeout = min(3600, max(1800, timeout))
        unique[key] = replace(check, id=previous.id if previous else check.id,
                              timeout=timeout, resources=sorted(resources))
    return list(unique.values())


def visible_text(output):
    """Remove parsed transport contracts from prose, without swallowing following text."""
    if not isinstance(output, str):
        return ""
    text = output
    for marker in reversed(list(re.finditer(r"(?m)^CHECK_PLAN:[ \t]*", text))):
        start = marker.end()
        payload = text[start:].lstrip()
        fence = payload.startswith("```json\n") or payload.startswith("```\n")
        if fence:
            payload = payload.split("\n", 1)[1].lstrip()
        try:
            _, length = json.JSONDecoder().raw_decode(payload)
            end = text.find(payload, start) + length
            if fence:
                closing = re.match(r"\s*```", text[end:])
                if closing:
                    end += closing.end()
        except ValueError:
            end = start  # Strip a bare marker, never guess where malformed prose ends.
        text = text[:marker.start()] + text[end:]
    text = re.sub(r"(?m)^ATTACH:[^\n]*(?:\n|$)", "", text)
    return text.strip() if text != output else text


def reported_payload(output):
    markers = list(re.finditer(r"(?m)^CHECK_PLAN:[ \t]*", output))
    if not markers:
        return ""
    text = output[markers[-1].end():].lstrip()
    # Accept a pretty-printed object, including a JSON fence after the marker.
    # Never search prose for a later object or fall back to an earlier plan.
    if text.startswith("```json\n"):
        text = text[len("```json\n"):].lstrip()
    elif text.startswith("```\n"):
        text = text[len("```\n"):].lstrip()
    try:
        _, end = json.JSONDecoder().raw_decode(text)
        return text[:end]
    except ValueError:
        return text


def _reported(output):
    text = reported_payload(output)
    if not text and not re.search(r"(?m)^CHECK_PLAN:", output):
        return [], None
    try:
        value, _ = json.JSONDecoder().raw_decode(text)
        return parse(value), None
    except json.JSONDecodeError:
        return [], "CHECK_PLAN must contain a valid JSON object with version 1 and a checks array"
    except (ValueError, TypeError, AttributeError) as error:
        return [], "CHECK_PLAN: " + str(error)


def reported(output):
    checks, error = _reported(output)
    if error:
        log.warning("Ignoring malformed agent check plan: %s", error)
    return checks


def reported_error(output):
    """A rejected plan is not successful verification or an empty valid plan."""
    return _reported(output)[1]


def remember(state, output):
    """Keep durable registrations; feature policy selects required execution separately."""
    reported, error = _reported(output)
    if error:
        if not state.get("reported_check_plan_error"):
            state["check_plan_repair_revision"] = state.get("check_plan_repair_revision", 0) + 1
        state["reported_check_plan_error"] = error
        import diagnostics
        payload = reported_payload(output)
        try:
            payload = json.dumps(diagnostics.safe(json.loads(payload)), ensure_ascii=False)
        except ValueError:
            payload = diagnostics.redact(payload)
        state["rejected_check_plan"] = payload
    elif re.search(r"(?m)^CHECK_PLAN:", output):
        state.pop("reported_check_plan_error", None)
        state.pop("rejected_check_plan", None)
        state.pop("check_plan_repair_round", None)
        current = {entry["id"]: entry for entry in state.get("reported_check_plan", [])}
        current.update({check.id: check.to_dict() for check in reported})
        state["reported_check_plan"] = list(current.values())


def discover(repo):
    repo = Path(repo)
    configured = load(repo)
    if configured is not None:
        return configured
    checks = []
    for directory in (".", "backend"):
        area = repo / directory
        if (area / "tests").is_dir() and (directory != "." or (area / "requirements.txt").exists()):
            # Preserve the venv launcher path: resolving its symlink runs the base
            # interpreter without pyvenv.cfg and loses all installed dependencies.
            candidates = [area / name / "bin/python" for name in (".venv", "venv")]
            python = next((p for p in candidates if p.is_file()), None)
            checks.append(Check("unit:" + directory,
                [str(python.absolute()) if python else sys.executable,
                 "-m", "unittest", "discover", "-s", "tests", "-v", *(["-t", "."] if directory == "." else [])], cwd=directory,
                inputs=[directory] if directory != "." else ["src", "tests", "requirements.txt"],
                reporter="unittest"))
    for directory in (".", "frontend"):
        package = repo / directory / "package.json"
        if not package.exists():
            continue
        scripts = json.loads(package.read_text()).get("scripts", {})
        for name in ("test", "lint", "build"):
            if name in scripts:
                checks.append(Check(f"{name}:{directory}", ["npm", "test"] if name == "test"
                    else ["npm", "run", name], cwd=directory, inputs=[directory],
                    reporter="node" if name == "test" and "node --test" in scripts[name] else
                    "eslint" if name == "lint" and "eslint" in scripts[name] else "text"))
    if (repo / "e2e/run.sh").exists():
        import harness_contract
        managed = harness_contract.load(repo)
        checks.extend(managed if managed is not None else [Check("e2e", ["./run.sh"], cwd="e2e", inputs=["*"],
                            resources=["harness:" + str(repo.resolve())], reporter="playwright")])
    return checks
