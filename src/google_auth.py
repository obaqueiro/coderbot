"""Shared credential loading for Gmail/Docs clients."""
import os
import tempfile

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

import config


class MissingCredentials(RuntimeError):
    """No Google token. A normal error (not SystemExit), so optional features such as
    Drive upload fall back instead of taking the whole process down."""


def load_credentials() -> Credentials:
    if not config.TOKEN_PATH.exists():
        raise MissingCredentials(f"Missing {config.TOKEN_PATH}. Run scripts/setup_oauth.py on the host first.")
    creds = Credentials.from_authorized_user_file(str(config.TOKEN_PATH), config.SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        _write_atomically(config.TOKEN_PATH, creds.to_json())
    return creds


def _write_atomically(path, text: str) -> None:
    """Several threads refresh the token; a torn token.json breaks every Google call."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
