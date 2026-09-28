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


@pytest.fixture(autouse=True)
def _quick_media_checks(monkeypatch):
    """Play/pause are re-checked a few times on the PC; no need to wait in tests."""
    from assistant.tools import video
    monkeypatch.setattr(video, "SETTLE_S", 0)


@pytest.fixture(autouse=True)
def _no_real_lessons_learned(tmp_path, monkeypatch):
    """Nor what Nova learned from corrections (data/lessons.json)."""
    from assistant.brain import lessons
    monkeypatch.setattr(lessons, "LESSONS_FILE", tmp_path / "lessons.json")
    monkeypatch.setattr(lessons, "_STORE", None)


@pytest.fixture
def local_settings(settings):
    """Settings for the local (Ollama) brain."""
    settings.brain.backend = "local"
    return settings


@pytest.fixture(autouse=True)
def _no_real_location(tmp_path, monkeypatch):
    """Nor the owner's city (data/location.json)."""
    from assistant.tools import quick
    monkeypatch.setattr(quick, "LOCATION_FILE", tmp_path / "location.json")


@pytest.fixture(autouse=True)
def _no_waiting_for_app_windows(monkeypatch):
    """open_app watches for the app's window for a few seconds on the PC; not in tests."""
    from assistant.tools import system
    monkeypatch.setattr(system, "OPEN_WAIT_S", 0)


@pytest.fixture(autouse=True)
def _no_waiting_for_windows_to_close(monkeypatch):
    from assistant.tools import pc
    monkeypatch.setattr(pc, "CLOSE_WAIT_S", 0)


@pytest.fixture(autouse=True)
def _no_waiting_in_the_browser(monkeypatch):
    """The browser control re-reads the tabs a few times on the PC; not in tests."""
    from assistant.tools import mybrowser
    monkeypatch.setattr(mybrowser, "STEP_S", 0)


@pytest.fixture(autouse=True)
def _no_real_blocked_sites(tmp_path, monkeypatch):
    """Nor the owner's blocked websites (data/blocked-sites.json)."""
    from assistant.tools import sitecheck
    monkeypatch.setattr(sitecheck, "BLOCKED_FILE", tmp_path / "blocked-sites.json")
    monkeypatch.setattr(sitecheck, "LAST_OPENED", {"host": ""})


@pytest.fixture(autouse=True)
def _no_real_claude_agent(monkeypatch):
    """Tests never start the real Claude Code (it's installed in some sandboxes); tests that want
    the work-it-out hand-off turn it on with a fake runner."""
    from assistant.brain.agent import Agent
    monkeypatch.setattr(Agent, "available", lambda self: False)
