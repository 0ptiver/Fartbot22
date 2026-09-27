"""Phone sign-in: a password plus a 6-digit code from an authenticator app (TOTP, RFC 6238).

- The password is stored only as a scrypt hash; the authenticator secret and the hash live in
  Windows Credential Manager (a user-only file when there's no keyring, e.g. in tests).
- Each signed-in phone gets its own random token (kept in a cookie). Only a SHA-256 of it is
  stored, in data/phone.json, so each phone can be signed out on its own.
- Five wrong tries in 15 minutes lock sign-in for 15 minutes, whoever is trying.
- A code can't be used twice (a code seen over someone's shoulder is already spent).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import struct
import time
from dataclasses import asdict, dataclass

from assistant.core.config import ROOT

STATE_FILE = ROOT / "data" / "phone.json"          # on/off + signed-in phones (no secrets)
CRED_FILE = ROOT / "data" / "phone_auth.json"      # only used when there's no keyring
USE_KEYRING = True
CRED_NAME = "PHONE_AUTH"

MIN_PASSWORD = 10
MAX_FAILS = 5
FAIL_WINDOW_S = 15 * 60
LOCK_S = 15 * 60
MAX_DEVICES = 10
_SCRYPT = {"n": 2 ** 15, "r": 8, "p": 1}


# --- TOTP ----------------------------------------------------------------------------------
def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def totp(secret: str, step: int) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    mac = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    off = mac[-1] & 0x0F
    return f"{(struct.unpack('>I', mac[off:off + 4])[0] & 0x7FFFFFFF) % 1_000_000:06d}"


def totp_step(secret: str, code: str, now: float | None = None, after: int = -1) -> int | None:
    """The time step the code belongs to (this 30 s, or one either side for clock drift),
    or None. Steps at or before `after` were already used."""
    code = "".join(ch for ch in str(code) if ch.isdigit())
    if len(code) != 6:
        return None
    step = int((time.time() if now is None else now) // 30)
    for s in (step, step - 1, step + 1):
        if s > after and hmac.compare_digest(totp(secret, s), code):
            return s
    return None


def otpauth_uri(secret: str, account: str, issuer: str = "Nova") -> str:
    from urllib.parse import quote
    return (f"otpauth://totp/{quote(issuer)}:{quote(account)}?secret={secret}"
            f"&issuer={quote(issuer)}&algorithm=SHA1&digits=6&period=30")


def qr_data_uri(text: str) -> str:
    import segno
    return segno.make(text, error="m").svg_data_uri(scale=6, border=2, dark="#0a0f1c", light="#ffffff")


# --- password ------------------------------------------------------------------------------
def hash_password(password: str) -> dict:
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(password.encode(), salt=salt, maxmem=128 * 1024 * 1024, dklen=32, **_SCRYPT)
    return {"salt": salt.hex(), "hash": h.hex(), **_SCRYPT}


def check_password(password: str, rec: dict) -> bool:
    try:
        h = hashlib.scrypt(password.encode(), salt=bytes.fromhex(rec["salt"]), n=rec["n"], r=rec["r"],
                           p=rec["p"], maxmem=128 * 1024 * 1024, dklen=32)
    except (KeyError, ValueError):
        return False
    return hmac.compare_digest(h.hex(), rec["hash"])


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# --- storage -------------------------------------------------------------------------------
def _load_creds() -> dict | None:
    raw = None
    if USE_KEYRING:
        from assistant.core.secrets import get_secret
        raw = get_secret(CRED_NAME)
    if raw is None and CRED_FILE.exists():
        raw = CRED_FILE.read_text(encoding="utf-8")
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


def _save_creds(creds: dict | None) -> None:
    raw = json.dumps(creds) if creds else ""
    if USE_KEYRING:
        try:
            import keyring
            from assistant.core.secrets import KEYRING_SERVICE
            if creds:
                keyring.set_password(KEYRING_SERVICE, CRED_NAME, raw)
            else:
                try:
                    keyring.delete_password(KEYRING_SERVICE, CRED_NAME)
                except Exception:
                    pass
            if CRED_FILE.exists():
                CRED_FILE.unlink()
            return
        except Exception:
            pass                                   # no keyring backend: the user-only file
    if not creds:
        if CRED_FILE.exists():
            CRED_FILE.unlink()
        return
    CRED_FILE.parent.mkdir(parents=True, exist_ok=True)
    CRED_FILE.write_text(raw, encoding="utf-8")
    os.chmod(CRED_FILE, 0o600)


@dataclass
class Device:
    id: str
    name: str
    token_hash: str
    created: float
    last_seen: float
    expires: float


class LoginError(Exception):
    def __init__(self, message: str, retry_after: int = 0):
        super().__init__(message)
        self.retry_after = retry_after


class PhoneAuth:
    def __init__(self, session_days: int = 30):
        self.session_s = session_days * 86400
        self._pending: str | None = None          # authenticator secret during setup
        self._pending_pw: dict | None = None
        self._fails: list[float] = []
        self._locked_until = 0.0
        self._last_step = -1
        self._seen_saved = 0.0
        self.on_lockout = None                    # callback(count) when sign-in gets locked
        self._configured: bool | None = None      # cached: reading Credential Manager isn't free
        self._load_state()

    # --- state file -----------------------------------------------------------------------
    def _load_state(self) -> None:
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        self.enabled = bool(data.get("enabled"))
        self._devices = [Device(**d) for d in data.get("devices", []) if isinstance(d, dict)]

    def _save_state(self) -> None:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps({"enabled": self.enabled, "devices": [asdict(d) for d in self._devices]},
                                  indent=1), encoding="utf-8")
        os.replace(tmp, STATE_FILE)

    @property
    def configured(self) -> bool:
        if self._configured is None:
            c = _load_creds()
            self._configured = bool(c and c.get("secret") and c.get("password"))
        return self._configured

    def set_enabled(self, on: bool) -> None:
        self.enabled = bool(on) and self.configured
        self._save_state()

    # --- setup ----------------------------------------------------------------------------
    def begin_setup(self, password: str, account: str) -> dict:
        if len(password) < MIN_PASSWORD:
            raise ValueError(f"Use at least {MIN_PASSWORD} characters for the phone password.")
        self._pending = new_secret()
        self._pending_pw = hash_password(password)
        uri = otpauth_uri(self._pending, account)
        return {"qr": qr_data_uri(uri), "secret": " ".join(self._pending[i:i + 4]
                                                           for i in range(0, len(self._pending), 4))}

    def finish_setup(self, code: str) -> bool:
        """The first code from the authenticator app proves it was added correctly."""
        if not self._pending or not self._pending_pw:
            return False
        step = totp_step(self._pending, code)
        if step is None:
            return False
        _save_creds({"secret": self._pending, "password": self._pending_pw})
        self._configured = None
        self._pending = self._pending_pw = None
        self._last_step = step
        self._devices = []                         # a new password signs every phone out
        self.enabled = True
        self._save_state()
        return True

    def cancel_setup(self) -> None:
        self._pending = self._pending_pw = None

    def reset(self) -> None:
        """Forget the password, the authenticator and every phone."""
        _save_creds(None)
        self._configured = None
        self._devices = []
        self.enabled = False
        self._save_state()

    # --- sign-in --------------------------------------------------------------------------
    def locked_for(self, now: float | None = None) -> int:
        now = time.time() if now is None else now
        return max(0, int(self._locked_until - now + 0.999))

    def login(self, password: str, code: str, device_name: str, now: float | None = None) -> str:
        now = time.time() if now is None else now
        wait = self.locked_for(now)
        if wait:
            raise LoginError(f"Too many wrong tries. Sign-in is locked for {wait // 60 + 1} more minutes.", wait)
        creds = _load_creds()
        if not (self.enabled and creds):
            raise LoginError("Phone access is switched off on the PC.")
        pw_ok = check_password(str(password)[:200], creds.get("password", {}))
        step = totp_step(creds["secret"], code, now, after=self._last_step)
        if not (pw_ok and step is not None):
            self._fails = [t for t in self._fails if now - t < FAIL_WINDOW_S] + [now]
            if len(self._fails) >= MAX_FAILS:
                self._locked_until = now + LOCK_S
                self._fails = []
                if self.on_lockout:
                    self.on_lockout(MAX_FAILS)
                raise LoginError("Too many wrong tries. Sign-in is locked for 15 minutes.", LOCK_S)
            raise LoginError("That password or code isn't right.")
        self._fails = []
        self._last_step = step
        token = secrets.token_urlsafe(32)
        self._devices = [d for d in self._devices if d.expires > now][-(MAX_DEVICES - 1):]
        self._devices.append(Device(secrets.token_hex(6), device_name[:60] or "Phone", _token_hash(token),
                                    now, now, now + self.session_s))
        self._save_state()
        return token

    def device_for(self, token: str | None, now: float | None = None) -> Device | None:
        if not token or not self.enabled:
            return None
        now = time.time() if now is None else now
        h = _token_hash(token)
        for d in self._devices:
            if hmac.compare_digest(d.token_hash, h) and d.expires > now:
                d.last_seen = now
                if now - self._seen_saved > 300:          # don't rewrite the file on every request
                    self._seen_saved = now
                    self._save_state()
                return d
        return None

    def devices(self) -> list[dict]:
        now = time.time()
        return [{"id": d.id, "name": d.name, "created": d.created, "last_seen": d.last_seen}
                for d in self._devices if d.expires > now]

    def revoke(self, device_id: str | None = None) -> int:
        """Sign one phone out (or all of them with None)."""
        before = len(self._devices)
        self._devices = [] if device_id is None else [d for d in self._devices if d.id != device_id]
        self._save_state()
        return before - len(self._devices)
