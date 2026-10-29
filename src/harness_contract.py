"""Optional target harness protocol; legacy run.sh remains supported."""
import json
from pathlib import Path
from check_plan import parse


def load(repo):
    path = Path(repo) / "e2e/codebot-harness.json"
    if not path.exists():
        return None
    value = json.loads(path.read_text())
    if value.get("version") != 1:
        raise ValueError("Unsupported harness contract version")
    checks = parse({"version": 1, "checks": value.get("checks", [])})
    for check in checks:
        check.preparation = value.get("preparation")
    return checks
