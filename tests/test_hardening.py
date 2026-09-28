"""Security hardening (owner: "make sure all security is tip-top shape")."""

import json

from fastapi.testclient import TestClient

from assistant.agent import tools_server
from assistant.brain.agent import Agent
from assistant.core.paths import EXECUTABLE_EXT
from assistant.hud import server as hud
from assistant.remote import auth as A
from assistant.remote import server as R
from assistant.tools.registry import Risk, ToolContext
from tests.test_phone import HOST, ORIGIN, FakeBrain, PhoneLoop, set_up, sign_in


def test_never_from_the_phone_whatever_the_settings_say(registry, settings):
    settings.safety.remote_blocked_tools = []            # e.g. a mistaken edit in local.yaml
    for name in ("teach", "voice_lock", "dictation", "replay_keys", "replay_click"):
        assert registry.effective_risk(name, remote=True) == Risk.BLOCKED, name


def test_opening_things_on_the_pc_from_the_phone_asks_first(registry):
    for name in ("open_app", "open_file", "open_website"):
        assert registry.effective_risk(name, remote=True) == Risk.CONFIRM, name
        assert registry.effective_risk(name, remote=False) == Risk.SAFE, name


async def test_files_that_run_code_or_mount_disks_ask_first_and_never_from_the_phone(registry, settings, tmp_path):
    assert {".iso", ".vhd", ".msc", ".appref-ms", ".settingcontent-ms", ".chm", ".xll", ".scf"} <= EXECUTABLE_EXT
    settings.safety.allowed_folders = [str(tmp_path)]
    f = tmp_path / "game.iso"
    f.write_bytes(b"x")
    asked = []

    async def no(tool, args):
        asked.append(tool)
        return False
    res = await registry.execute("open_file", {"path": str(f)}, ToolContext(settings, confirm=no))
    assert res.is_error and asked                          # asked, and "no" means it didn't open
    phone = ToolContext(settings, remote=True, confirm=lambda *a: _yes())
    res = await registry.execute("open_file", {"path": str(f)}, phone)
    assert res.is_error and "remote" in str(res.content)


async def _yes():
    return True


def test_claude_gets_no_file_tools_and_no_web_fetch(settings, tmp_path):
    """A web page could try to talk Claude into reading documents and sending them away."""
    assert not {"read_file", "find_files", "open_file"} & set(tools_server.AGENT_TOOLS)
    fake = type("E", (), {"executable": lambda self: "claude"})()      # no real CLI in tests
    line = Agent(settings, expert=fake).command(tmp_path / "mcp.json")
    tools = line[line.index("--tools") + 1]
    allowed = line[line.index("--allowedTools") + 1]
    assert "WebFetch" not in tools and "WebFetch" not in allowed and "--dangerously-skip-permissions" not in line


def _app(settings, loop=None, chats=None):
    auth = A.PhoneAuth()
    secret = set_up(auth)
    app = R.create_phone_app(settings, auth, loop or PhoneLoop(None), hud.Hub(), trust_test_client=True, chats=chats)
    return TestClient(app), secret


def test_sign_in_reads_only_a_small_body(settings):
    client, _ = _app(settings)
    big = json.dumps({"password": "x" * 10000, "code": "123456"})
    r = client.post("/api/login", content=big, headers={**ORIGIN, "content-type": "application/json"})
    assert r.status_code == 400


def test_voice_uploads_are_capped_and_one_at_a_time(settings, registry, monkeypatch):
    from assistant.remote import voice as V
    chats = {}
    client, secret = _app(settings, PhoneLoop(FakeBrain(settings, registry)), chats=chats)
    sign_in(client, secret)
    monkeypatch.setattr(V, "MAX_BYTES", 1000)
    assert client.post("/api/voice", content=b"x" * 2000, headers=ORIGIN).status_code == 413
    assert chats == {}                                          # refused before anything was set up
    client.post("/api/voice", content=b"x", headers=ORIGIN)       # no hearing in this fake: just makes the chat
    chat = next(iter(chats.values()))
    chat.hearing = True
    assert client.post("/api/voice", content=b"x", headers=ORIGIN).status_code == 429


def test_pages_carry_strict_headers(settings):
    client, _ = _app(settings)
    h = client.get("/", headers=HOST).headers
    assert "frame-ancestors 'none'" in h["content-security-policy"] and h["x-frame-options"] == "DENY"
    assert "camera=()" in h["permissions-policy"] and h["cross-origin-opener-policy"] == "same-origin"
    assert "strict-transport-security" not in h           # plain http in tests: only sent over https
