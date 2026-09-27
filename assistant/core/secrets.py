"""Secrets: environment / config/.env first, then Windows Credential Manager (keyring)."""

from __future__ import annotations

import os

from dotenv import load_dotenv

from assistant.core.config import ROOT

KEYRING_SERVICE = "pc-assistant"

load_dotenv(ROOT / "config" / ".env")


def get_secret(name: str) -> str | None:
    value = os.environ.get(name)
    if value:
        return value
    try:
        import keyring

        return keyring.get_password(KEYRING_SERVICE, name)
    except Exception:  # no keyring backend available (e.g. headless Linux)
        return None


def set_secret(name: str, value: str) -> None:
    import keyring

    keyring.set_password(KEYRING_SERVICE, name, value)


def local_client_token() -> str:
    """Token local clients (CLI, HUD) present to the core server.

    Generated once and kept in Windows Credential Manager. Falls back to a
    user-only file when no keyring backend exists (e.g. headless Linux).
    """
    import secrets as _secrets

    token = get_secret("LOCAL_CLIENT_TOKEN")
    if token:
        return token
    path = ROOT / "data" / "local_token"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    token = _secrets.token_urlsafe(32)
    try:
        set_secret("LOCAL_CLIENT_TOKEN", token)
        if get_secret("LOCAL_CLIENT_TOKEN") == token:
            return token
    except Exception:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token, encoding="utf-8")
    os.chmod(path, 0o600)
    return token
