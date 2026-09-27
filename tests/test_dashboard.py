"""The dashboard: Nova drives its own window, and the live feed gets short tool details."""

import pytest

from assistant.brain.intents import match_intent
from assistant.hud import server as hud
from assistant.tools import hudnav
from assistant.tools.registry import ToolContext, ToolError
from assistant.voice.pipeline import _brief_args


@pytest.mark.parametrize("text,page", [
    ("show me my timers", "timers"), ("open your brain", "brain"), ("go back to the dashboard", "home"),
    ("show me what you remember", "brain"), ("open the voice settings", "voice"), ("show the activity log", "activity"),
])
def test_nova_can_switch_its_window(text, page):
    assert match_intent(text) == ("show_page", {"page": page})


def test_other_shows_are_not_pages():
    assert match_intent("show the grid")[0] == "mouse_grid"
    assert match_intent("show me the desktop")[0] == "window"
    assert match_intent("open spotify")[0] == "open_app"


async def test_show_page_tells_the_window(settings):
    seen = []
    voice = type("V", (), {"on_event": lambda self, ev: seen.append(ev)})()
    ctx = ToolContext(settings, services={"voice": voice})
    assert await hudnav.show_page({"page": "timers"}, ctx) == "Here's your timers."
    assert seen == [{"type": "navigate", "page": "timers"}]
    with pytest.raises(ToolError):
        await hudnav.show_page({"page": "timers"}, ToolContext(settings))


def test_live_feed_gets_short_plain_arguments():
    out = _brief_args({"name": "discord", "text": "x" * 500, "n": 3, "nested": {"a": 1}, "actions": ["play", "fullscreen"]})
    assert out == {"name": "discord", "text": "x" * 80, "n": 3, "actions": ["play", "fullscreen"]}
    assert _brief_args(None) == {}


def test_app_labels_for_now_playing():
    assert hud._app_label("Spotify.exe") == "Spotify"
    assert hud._app_label("308046B0AF4A39CB") == "Firefox"
    assert hud._app_label("chrome.exe") == "Chrome"
    assert hud._app_label("msedge.exe") == "Edge"


def test_now_playing_buttons_use_the_exact_media_tool(settings, registry):
    from fastapi.testclient import TestClient
    from tests.test_hud import KEY, FakeLoop, connect
    import time
    calls = []
    loop = FakeLoop()
    loop.brain = type("B", (), {"registry": type("R", (), {
        "execute": lambda self, name, args, ctx: _record(calls, name, args)})()})()
    loop.ctx = ToolContext(settings)
    client = TestClient(hud.create_hud_app(settings, loop, hud.Hub(), KEY, None, trust_test_client=True))
    ws = connect(client)
    ws.send_json({"type": "media_cmd", "action": "pause"})
    ws.send_json({"type": "media_cmd", "action": "rm -rf"})             # not a button: ignored
    time.sleep(0.3)
    ws.close()
    assert calls == [("media", {"action": "pause"})]


async def _record(calls, name, args):
    from assistant.tools.registry import ToolResult
    calls.append((name, args))
    return ToolResult("Paused.")
