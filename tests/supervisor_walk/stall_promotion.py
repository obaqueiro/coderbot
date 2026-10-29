"""scripts/supervisor.py with the promotion stalled after its first file: the walk then
kill -9s it in the torn state (src/bug.py new, the regression test not yet copied).

Usage: stall_promotion.py <scripts/supervisor.py> <supervisor args...>"""
import os
from pathlib import Path
import sys
import time

sys.argv = sys.argv[1:]
sys.path.insert(0, str(Path(sys.argv[0]).resolve().parent))
import selfheal  # noqa: E402

replace = selfheal.Repairs._replace


def stalled(source, destination, *, expected=None):
    replace(source, destination, expected=expected)
    data = Path(os.environ["CODEBOT_DATA_DIR"])
    if (selfheal.read_json(data / "supervisor/promotion.json", {}) or {}).get("status") == "promoting":
        (data / "stalled").touch()
        while True:
            time.sleep(1)


selfheal.Repairs._replace = staticmethod(stalled)
import supervisor  # noqa: E402

supervisor.main()
