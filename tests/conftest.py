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
