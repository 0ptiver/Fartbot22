import pytest

from assistant.core.config import Settings
from assistant.tools import build_registry
from assistant.tools.registry import AuditLog, ToolContext


@pytest.fixture
def settings(tmp_path):
    s = Settings()
    s.safety.audit_log = str(tmp_path / "audit.jsonl")
    s.memory.path = str(tmp_path / "memory.db")
    s.tools.app_aliases = {"spotify": "spotify:"}
    return s


@pytest.fixture
def registry(settings):
    return build_registry(settings, AuditLog(settings.safety.audit_path()))


@pytest.fixture
def ctx(settings):
    return ToolContext(settings)


@pytest.fixture(autouse=True)
def _no_real_voice_file(tmp_path, monkeypatch):
    """Nothing in the tests may touch the owner's saved voice (data/voice.json)."""
    from assistant.voice import voicedesign
    monkeypatch.setattr(voicedesign, "VOICE_FILE", tmp_path / "voice.json")


@pytest.fixture(autouse=True)
def _no_real_lessons(tmp_path, monkeypatch):
    """Nor the lessons taught by showing (data/learned.json)."""
    from assistant.tools import teach
    monkeypatch.setattr(teach, "LEARNED_FILE", tmp_path / "learned.json")


@pytest.fixture(autouse=True)
def _no_real_phone_auth(tmp_path, monkeypatch):
    """Nor phone sign-in (Credential Manager + data/phone.json)."""
    from assistant.remote import auth
    monkeypatch.setattr(auth, "USE_KEYRING", False)
    monkeypatch.setattr(auth, "CRED_FILE", tmp_path / "phone_auth.json")
    monkeypatch.setattr(auth, "STATE_FILE", tmp_path / "phone.json")
