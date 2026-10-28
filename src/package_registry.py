"""Scoped private package access without routing public tools through private auth."""
from pathlib import Path
import os


def public_runtime_npmrc(source, destination):
    source, destination = Path(source), Path(destination)
    lines = source.read_text().splitlines() if source.is_file() else []
    # Preserve scopes and credential entries byte-for-byte. Only the global registry
    # changes; private scoped requests still use their configured URL and token.
    lines = [line for line in lines if not line.strip().startswith("registry=")]
    lines.append("registry=https://registry.npmjs.org/")
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(destination, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as output:
        output.write("\n".join(lines) + "\n")
    return destination
