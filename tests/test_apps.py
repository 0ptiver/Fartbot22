"""Any app by voice: look, click by name or number, type into a box, press keys, always in the
right window (owner: "control ... any app for that matter effortlessly")."""

import pytest

from assistant.brain.intents import match_intent
from assistant.tools import apps, grid as G, keyboard as K, pc, uia
from assistant.tools.registry import ToolContext, ToolError


class Desk:
    def __init__(self, focus_ok=True):
        self.front, self.focus_ok = 3, focus_ok
        self.wins = [pc.Win(3, "YouTube — Mozilla Firefox", "firefox.exe"), pc.Win(4, "#general | Friends - Discord", "Discord.exe"),
                     pc.Win(5, "Windows PowerShell", "WindowsTerminal.exe")]
        self.log = []

    def list(self):
        return self.wins

    def active(self):
        return next(w for w in self.wins if w.hwnd == self.front)

    def foreground(self):
        return self.front

    def focus(self, hwnd):
        if self.focus_ok:
            self.front = hwnd
        return self.focus_ok

    def elements(self, hwnd):
        if hwnd == 4:
            return [uia.Element("Message #general", "box", G.Region(400, 900, 800, 40)),
                    uia.Element("general", "link", G.Region(80, 200, 150, 24)),
                    uia.Element("Mute", "button", G.Region(60, 950, 30, 30)),
                    uia.Element("", "button", G.Region(10, 10, 20, 20))]
        return []

    # keyboard + mouse
    def type(self, text):
        self.log.append(("type", text, self.front))

    def combo(self, vks):
        self.log.append(("keys", K.canonical(vks), self.front))

    def move(self, x, y):
        self.log.append(("move", x, y))

    def click(self, button="left", double=False):
        self.log.append(("click", button, double, self.front))


@pytest.fixture
def desk(monkeypatch):
    d = Desk()
    monkeypatch.setattr(pc, "WINDOWS", d)
    monkeypatch.setattr(K, "KEYBOARD", d)
    monkeypatch.setattr(uia, "UIA", type("U", (), {"elements": lambda s, h: d.elements(h)})())
    monkeypatch.setattr(apps.asyncio, "sleep", _no_sleep)
    return d


async def _no_sleep(*a):
    return None


def ctx(settings, d, **kw):
    return ToolContext(settings, services={"grid": G.GridController(mouse=d, overlay=type("O", (), {"hide": lambda s: None})())}, **kw)


async def test_look_then_click_by_number_in_another_app(settings, desk):
    out = await apps.app_control({"action": "look", "app": "discord"}, ctx(settings, desk))
    assert desk.front == 4 and '[1] link "general"' in out and '[2] box "Message #general"' in out
    assert '""' not in out                                           # nameless things aren't listed
    out = await apps.app_control({"action": "click", "target": "1", "app": "discord"}, ctx(settings, desk))
    assert out == "Clicked general in Discord." and ("move", 155, 212) in desk.log


async def test_type_into_a_box_by_name(settings, desk):
    assert match_intent("type gg into the message box") == ("app", {"action": "type", "text": "gg", "target": "message box"})
    out = await apps.app_control({"action": "type", "app": "discord", "target": "message box", "text": "gg",
                                  "enter": True}, ctx(settings, desk))
    assert out == 'Typed "gg" into Message #general and pressed Enter.'
    assert [e for e in desk.log if e[0] in ("type", "keys")] == [("type", "gg", 4), ("keys", "enter", 4)]


async def test_keys_go_to_the_named_app_not_the_one_in_front(settings, desk):
    out = await apps.app_control({"action": "press", "app": "discord", "keys": "ctrl+k"}, ctx(settings, desk))
    assert out == "Pressed ctrl+k in Discord." and desk.log == [("keys", "ctrl+k", 4)]


async def test_refused_switch_presses_nothing(settings, desk):
    desk.focus_ok = False
    with pytest.raises(ToolError, match="wouldn't let me switch to Discord"):
        await apps.app_control({"action": "press", "app": "discord", "keys": "enter"}, ctx(settings, desk))
    assert desk.log == []


async def test_unknown_name_lists_what_is_there(settings, desk):
    with pytest.raises(ToolError, match="can't see 'deafen'.*general.*Mute"):
        await apps.app_control({"action": "click", "app": "discord", "target": "deafen"}, ctx(settings, desk))


async def test_typing_into_a_terminal_still_asks(settings, desk):
    asked = []

    async def confirm(tool, args):
        asked.append(tool)
        return False
    with pytest.raises(ToolError, match="declined"):
        await apps.app_control({"action": "type", "app": "powershell", "text": "rm -r ~"}, ctx(settings, desk, confirm=confirm))
    assert asked == ["type_text"] and not [e for e in desk.log if e[0] == "type"]


def test_the_app_tool_is_blocked_from_a_phone(registry):
    registry.settings.phone.pc_control = "off"          # the "phone can't control the PC" setting
    assert registry.effective_risk("app", remote=True).value == "blocked"
    assert registry.effective_risk("my_browser", remote=True).value == "blocked"


@pytest.mark.parametrize("said,expected", [
    ("click send in discord", {"action": "click", "target": "send", "app": "discord"}),
    ("in spotify click shuffle", {"action": "click", "target": "shuffle", "app": "spotify"}),
    ("type gg in discord", {"action": "type", "text": "gg", "app": "discord"}),
    ("right click the file in explorer", {"action": "right_click", "target": "file", "app": "explorer"}),
])
def test_phrases(said, expected):
    assert match_intent(said) == ("app", expected)


def test_plain_clicks_and_typing_are_unchanged():
    assert match_intent("click subscribe") == ("click_element", {"name": "subscribe"})
    assert match_intent("type hello world") == ("type_text", {"text": "hello world"})
    assert match_intent("click on this page")[0] != "app"
