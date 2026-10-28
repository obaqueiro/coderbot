"""Make src/ importable so tests can `import main`, `import config`, ...

Run from the repo root:  python3 -m unittest discover -s tests -t .
"""
import os
import sys
import tempfile
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# Point config.DATA_DIR at a throwaway dir before config is ever imported, so no test
# that reaches a data-dir write (state.json, the agent log, ...) can poison the real
# data/ of a live deployment sharing this checkout. Individual modules still override
# config.STATE_PATH; this is the backstop for everything else.
os.environ["CODEBOT_DATA_DIR"] = tempfile.mkdtemp(prefix="codebot-test-data-")
for variable in ("CODEBOT_SUPERVISOR_DIR", "CODEBOT_WORKER_BOOT", "CODEBOT_RUNTIME_EPOCH"):
    os.environ.pop(variable, None)

# Several legacy test modules import main under patch.dict(sys.modules, ...).
# Keep Markdown and its extension base classes loaded before those temporary module
# patches, so later imports do not create incompatible Extension class identities.
try:
    import markdown  # noqa: F401
    import markdown.extensions.fenced_code  # noqa: F401
    import markdown.extensions.tables  # noqa: F401
    import markdown.extensions.sane_lists  # noqa: F401
except ImportError:
    pass  # Minimal host-side tests still run without optional runtime dependencies.
