"""Read-only verification of project-scoped Compose cleanup."""
import json
import os
import re
import shlex

from workspace import WorkspaceError, contained


class HarnessBlocked(WorkspaceError):
    """The agent can repair the harness/configuration instead of retrying it."""


def compose_commands(scripts):
    """Extract explicit Compose invocations, including quoted COMPOSE variables.

    Deliberately do not execute/evaluate shell. Unknown dynamic configurations
    cannot establish a cleanup boundary and must be made explicit by the agent.
    """
    commands = []
    for body in scripts:
        for line in body.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            assignment = re.fullmatch(r'\w+=(["\'])(docker compose.*?)\1', line)
            if assignment:
                line = assignment[2]
            if not line.startswith("docker compose "):
                continue
            try:
                tokens = shlex.split(line)
            except ValueError as error:
                raise HarnessBlocked("Cannot parse Compose command; use explicit project and file arguments") from error
            project, files, env_files, position = None, [], [], 2
            while position < len(tokens):
                token = tokens[position]
                option, equals, inline = token.partition("=")
                if option in ("-p", "--project-name", "-f", "--file", "--env-file"):
                    if equals:
                        value = inline
                        position += 1
                    elif position + 1 < len(tokens):
                        value = tokens[position + 1]
                        position += 2
                    else:
                        raise HarnessBlocked(f"Compose option {option} needs a value")
                    if option in ("-p", "--project-name"):
                        project = value
                    elif option == "--env-file":
                        env_files.append(value)
                    else:
                        files.append(value)
                elif token.startswith("-"):
                    raise HarnessBlocked(f"Unsupported Compose option {token}; cannot establish the cleanup configuration")
                else:
                    break
            if not project or not files or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", project):
                raise HarnessBlocked("Compose cleanup requires literal --project-name and --file arguments")
            if not re.search(r"(?:^|[-_])(?:e2e|test)(?:$|[-_])", project):
                raise HarnessBlocked("Compose cleanup must use a dedicated test/e2e project name")
            commands.append((project, tuple(files), tuple(env_files)))
    return list(dict.fromkeys(commands))


def validate_resources(value, project):
    if not isinstance(value, dict) or value.get("name") != project:
        raise HarnessBlocked("Resolved Compose project does not match the test project")
    for section in ("volumes", "networks"):
        for key, resource in value.get(section, {}).items():
            resource = resource or {}
            if resource.get("external") or resource.get("driver_opts"):
                raise HarnessBlocked(f"Test {section} {key} uses external/shared resources")
            name = resource.get("name", project + "_" + key)
            if name != project and not name.startswith(project + "_"):
                raise HarnessBlocked(f"Test {section} {key} is not scoped to project {project}")
    for name, service in value.get("services", {}).items():
        fixed = service.get("container_name")
        if fixed and not fixed.startswith(project + "_") and not fixed.startswith(project + "-"):
            raise HarnessBlocked(f"Test service {name} uses a shared container name")
        for volume in service.get("volumes", []):
            if isinstance(volume, dict) and volume.get("type") == "volume" and volume.get("source") not in value.get("volumes", {}):
                raise HarnessBlocked(f"Test service {name} has an undeclared volume")


def validate_docker(context, command):
    mapping = json.loads(context.manifest.docker_json)
    try:
        current = command(context.root, ["docker", "context", "show"])
    except (RuntimeError, OSError) as error:
        raise HarnessBlocked("Cannot identify Docker context; check daemon availability") from error
    if mapping.get("context") and mapping["context"] != current:
        raise HarnessBlocked(f"Docker context mismatch: expected {mapping['context']}, got {current}")
    endpoint = os.environ.get("DOCKER_HOST")
    if not endpoint:
        try:
            endpoint = json.loads(command(context.root, ["docker", "context", "inspect", current,
                                  "--format", "{{json .Endpoints.docker.Host}}"] ))
        except (ValueError, RuntimeError, OSError) as error:
            raise HarnessBlocked("Cannot identify Docker endpoint; check context configuration") from error
    if not isinstance(endpoint, str):
        raise HarnessBlocked("Docker endpoint must identify a local socket or mapped remote host")
    if not endpoint.startswith("unix://") and not mapping.get("host_root"):
        raise HarnessBlocked("Remote Docker tests require explicit host_root mapping; local socket tests use mounted host paths")


def _compose_path(context, value):
    # Expand only root references and simple environment fallbacks; never eval
    # shell text or expand arbitrary command substitutions.
    value = re.sub(r"\$\{(?:ROOT|root)\}|\$(?:ROOT|root)(?=/|$)", lambda _: str(context.root), value)
    default = re.fullmatch(r"\$\{([A-Za-z_][A-Za-z0-9_]*):-([^{}]*)\}", value)
    if default:
        value = os.environ.get(default[1]) or default[2]
    variable = re.fullmatch(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)", value)
    if variable:
        value = os.environ.get(variable[1] or variable[2], "")
    if not value or "$" in value or "`" in value:
        raise HarnessBlocked("Compose path is dynamic or unset; configure an explicit workspace file")
    try:
        path = contained(context.root, value)
    except WorkspaceError as error:
        raise HarnessBlocked("Compose input must stay inside the configured workspace") from error
    if not path.is_file():
        raise HarnessBlocked(f"Compose input file not found: {path.name}")
    return path


def validate_cleanup(context, scripts, command):
    commands = compose_commands(scripts)
    if not commands:
        raise HarnessBlocked("destructive Docker cleanup has no explicit dedicated Compose project")
    for project, files, env_files in commands:
        argv = ["docker", "compose", "--project-name", project]
        for file in files:
            argv.extend(["--file", str(_compose_path(context, file))])
        for file in env_files:
            argv.extend(["--env-file", str(_compose_path(context, file))])
        # config is read-only; never bring up/down containers during validation.
        try:
            value = json.loads(command(context.root, [*argv, "config", "--format", "json"]))
        except (ValueError, RuntimeError) as error:
            raise HarnessBlocked("Cannot resolve test Compose configuration; check env/files before running cleanup") from error
        validate_resources(value, project)
