"""Probe the installed HTTP contract without model calls or production state.

Run with the pinned executable in the container. Uses isolated XDG paths and a
scratch project; never reads account credentials or the live OpenCode database.
"""
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
import urllib.request
import urllib.error


def probe(executable="opencode", plugin=None, exercise=None, extra_config=None):
    with tempfile.TemporaryDirectory(prefix="codebot-runtime-probe-") as scratch:
        root = Path(scratch)
        env = {**os.environ, **{f"XDG_{name}_HOME": str(root / name.lower())
                               for name in ("DATA", "CONFIG", "CACHE", "STATE")},
               "OPENCODE_CONFIG_CONTENT": json.dumps({"permission": "allow", "plugin": [],
                   "share": "disabled", "autoupdate": False, "lsp": False, "formatter": False,
                   **({"plugin": [str(plugin)]} if plugin else {}), **(extra_config or {})}),
               "OPENCODE_DISABLE_EXTERNAL_SKILLS": "1",
               "OPENCODE_DISABLE_DEFAULT_PLUGINS": "1"}
        env.pop("OPENCODE_CONFIG", None)
        env.pop("OPENCODE_SERVER_PASSWORD", None)
        with (root / "server.log").open("w+") as output:
            process = subprocess.Popen([executable, "serve", "--port", "0"], cwd=root,
                env=env, stdout=output, stderr=output, start_new_session=True)
            try:
                address = None
                deadline = time.monotonic() + 45
                while time.monotonic() < deadline:
                    output.seek(0)
                    for line in output.read().splitlines():
                        if "http://127.0.0.1:" in line:
                            address = line[line.index("http://127.0.0.1:"):].split()[0]
                    if address:
                        break
                    if process.poll() is not None:
                        raise RuntimeError("Isolated OpenCode server exited before readiness")
                    time.sleep(.1)
                if not address:
                    raise TimeoutError("Isolated OpenCode server readiness exceeded 45s")

                def request(route, data=None, method=None):
                    req = urllib.request.Request(address + route,
                        data=json.dumps(data).encode() if data is not None else None,
                        headers={"Content-Type": "application/json", "x-opencode-directory": str(root)},
                        method=method)
                    try:
                        with urllib.request.urlopen(req, timeout=15) as response:
                            return json.load(response)
                    except urllib.error.HTTPError as error:
                        raise RuntimeError(error.read().decode()) from error

                config = request("/config")
                request.environment = env
                assert config["permission"] == {"*": "allow"} or config["permission"] == "allow"
                allow = [{"permission": "*", "pattern": "*", "action": "allow"}]
                parent = request("/session", {"title": "probe", "permission": allow})
                updated = request("/session/" + parent["id"], {"permission": allow}, "PATCH")
                assert updated["permission"][-1] == allow[0]
                assert isinstance(request("/session/" + parent["id"] + "/children"), list)
                assert isinstance(request("/permission"), list)
                exercise_result = exercise(request, root, parent) if exercise else None
                request("/session/" + parent["id"], method="DELETE")
                version = subprocess.run([executable, "--version"], env=env, cwd=root,
                    capture_output=True, text=True, errors="replace", timeout=15, check=True).stdout.strip()
                return {"version": version, "config": "valid", "session_update": "valid",
                        "children": "valid", "permission_list": "valid",
                        **({"exercise": exercise_result} if exercise else {})}
            finally:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
                except ProcessLookupError:
                    pass


if __name__ == "__main__":
    print(json.dumps(probe()))
