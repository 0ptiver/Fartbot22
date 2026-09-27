"""Secrets: environment / config/.env first, then Windows Credential Manager (keyring)."""

from __future__ import annotations

import os

from dotenv import load_dotenv

from vesper.core.config import ROOT

KEYRING_SERVICE = "vesper"

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
