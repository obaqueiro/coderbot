"""Shared credential loading for Gmail/Docs clients."""
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

import config
import persistence


class MissingCredentials(RuntimeError):
    """No Google token. A normal error (not SystemExit), so optional features such as
    Drive upload fall back instead of taking the whole process down."""


def load_credentials() -> Credentials:
    if not config.TOKEN_PATH.exists():
        raise MissingCredentials(f"Missing {config.TOKEN_PATH}. Run scripts/setup_oauth.py on the host first.")
    creds = Credentials.from_authorized_user_file(str(config.TOKEN_PATH), config.SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        # Several threads refresh the token; a torn token.json breaks every Google call.
        persistence.atomic_write(config.TOKEN_PATH, creds.to_json(), mode=0o600)
    return creds
