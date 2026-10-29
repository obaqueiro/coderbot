"""Deploy a tested Coderbot release without overwriting app/target checkouts.

Run on the deployment host as a user with passwordless sudo Docker access.
Backups contain private runtime config and are created mode 0700/0600.
"""
import argparse
import json
from pathlib import Path
import subprocess
import time


def command(argv, cwd=None):
    return subprocess.run(argv, cwd=cwd, check=True, text=True, errors="replace", capture_output=True, timeout=180)


def preserved_mounts(inspected, release=None):
    mounts = [{"type": "bind", "source": m["Source"], "target": m["Destination"],
               "read_only": not m["RW"]} for m in inspected["Mounts"] if m["Type"] == "bind"]
    if release:
        for mount in mounts:
            if mount["target"] == "/app":
                mount["source"] = str(release)
    return mounts


def deploy(app, release, image):
    app, release = Path(app).resolve(), Path(release).resolve()
    if not (app / "data/state.json").is_file() or not (release / "src/main.py").is_file():
        raise ValueError("Expected existing app state and tested release")
    backups = app.parent / "rollout-backups"
    backups.mkdir(exist_ok=True, mode=0o700)
    backup = backups / time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    backup.mkdir(mode=0o700)
    docker = ["sudo", "-n", "docker"]
    inspected = json.loads(command([*docker, "inspect", "app-codebot-1"]).stdout)[0]
    private = backup / "container.json"
    private.write_text(json.dumps(inspected))
    private.chmod(0o600)
    old_image = inspected["Image"]
    command([*docker, "tag", old_image, "codebot-performance-rollback:" + backup.name.lower()])
    # Freeze the task before copying durable cursors. Docker init terminates/reaps
    # old agent/test children; the target checkout is not edited by this script.
    command([*docker, "stop", "--time", "30", "app-codebot-1"])
    command(["sudo", "-n", "python3", "-c", "import pathlib,shutil,sys,sqlite3; "
        "src=pathlib.Path(sys.argv[1]); dst=pathlib.Path(sys.argv[2]); "
        "[(shutil.copy2(p,dst/p.name)) for p in src.iterdir() if p.is_file() and p.suffix in ('.json','.sqlite')]; "
        "[(sqlite3.connect(str(p)).backup(sqlite3.connect(str(dst/p.name)))) for p in src.glob('*.sqlite')]",
        str(app / "data"), str(backup)])
    before = json.loads(command(["sudo", "-n", "python3", "-c",
        "import json,sys; print(json.dumps(json.load(open(sys.argv[1]))))", str(app / "data/state.json")]).stdout)
    # Reuse the original env file, credentials and persistent data. Compose replaces
    # the /app bind only; the original checkout and its uncommitted work are intact.
    override = release / "rollout.json"
    mounts = preserved_mounts(inspected, release)
    override.write_text(json.dumps({"services": {"codebot": {"image": image,
        "environment": {"CODEBOT_OPENCODE_TRANSPORT": "http"},
        "volumes": mounts}}}))
    compose = [*docker, "compose", "-p", "app", "-f", str(app / "docker-compose.yml")]
    if (app / "docker-compose.override.yml").exists():
        compose.extend(["-f", str(app / "docker-compose.override.yml")])
    compose.extend(["-f", str(override)])
    rollback = backup / "rollback.json"
    rollback.write_text(json.dumps({"services": {"codebot": {"image": old_image,
        "volumes": preserved_mounts(inspected)}}}))
    try:
        command([*compose, "up", "-d", "--no-build", "--force-recreate", "codebot"], cwd=app)
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            state = json.loads(command([*docker, "inspect", "app-codebot-1"]).stdout)[0]["State"]
            if state["Status"] != "running":
                raise RuntimeError("Coderbot did not remain running after rollout")
            if state.get("Health", {}).get("Status") == "healthy":
                break
            time.sleep(2)
        else:
            raise TimeoutError("Coderbot did not become healthy after rollout")
        result = {"image": image, "release": str(release), "backup": str(backup),
                  "task_before": {k: before.get(k) for k in ("state", "branch", "session_id")},
                  "original_app_preserved": True, "target_modified_by_rollout": False}
        (backup / "result.json").write_text(json.dumps(result, indent=2))
        return result
    except BaseException:
        # Keep durable state and restore the original code/image mounts.
        original = compose[:-2]
        command([*original, "-f", str(rollback), "up", "-d", "--no-build", "--force-recreate", "codebot"], cwd=app)
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("app")
    parser.add_argument("release")
    parser.add_argument("image")
    args = parser.parse_args()
    print(json.dumps(deploy(args.app, args.release, args.image), indent=2))
