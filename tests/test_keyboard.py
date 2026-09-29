"""Keyboard by voice: typing, keys, shortcuts, 'this window', and the terminal safety check."""

import pytest

from assistant.brain.intents import match_intent
from assistant.tools import keyboard as K, pc
from assistant.tools.registry import ToolContext, ToolError


class FakeKeyboard:
    def __init__(self):
        self.typed, self.combos = [], []

    def type(self, text):
        self.typed.append(text)

    def combo(self, vks):
        self.combos.append(vks)


class FakeWindows:
    def __init__(self, active):
        self._active, self.calls = active, []

    def active(self):
        return self._active

    def list(self):
        return [self._active]

    def show(self, hwnd, how):
        self.calls.append((hwnd, how))


NOTEPAD = pc.Win(5, "notes.txt - Notepad", "notepad.exe")
TERMINAL = pc.Win(6, "Windows PowerShell", "WindowsTerminal.exe")


@pytest.fixture
def kb(monkeypatch):
    fake = FakeKeyboard()
    monkeypatch.setattr(K, "KEYBOARD", fake)
    return fake


def use(monkeypatch, win):
    fake = FakeWindows(win)
    monkeypatch.setattr(pc, "WINDOWS", fake)
    return fake


def test_parse_keys():
    assert K.parse_keys("ctrl+shift+t") == [0x11, 0x10, ord("T")]
    assert K.parse_keys("control c") == [0x11, ord("C")]
    assert K.parse_keys("alt f 4") == [0x12, 0x73]
    assert K.parse_keys("page down") == [0x22] and K.parse_keys("windows key") == [0x5B]
    with pytest.raises(ToolError, match="don't know"):
        K.parse_keys("hyper q")
    with pytest.raises(ToolError, match="One key"):
        K.parse_keys("a b")


async def test_typing_and_keys(settings, registry, kb, monkeypatch):
    use(monkeypatch, NOTEPAD)
    res = await registry.execute("type_text", {"text": "Dear John,\nhello"}, ToolContext(settings))
    assert not res.is_error and kb.typed == ["Dear John,\nhello"]
    res = await registry.execute("press_keys", {"keys": "tab", "times": 3}, ToolContext(settings))
    assert res.content == "Pressed tab 3 times in Notepad." and kb.combos == [[0x09]] * 3


async def test_terminal_asks_first(settings, registry, kb, monkeypatch):
    settings.phone.pc_control = "off"          # the "phone can't control the PC" setting
    use(monkeypatch, TERMINAL)
    asked = []

    async def no(tool, args):
        asked.append(registry.describe(tool, args))
        return False
    res = await registry.execute("type_text", {"text": "del *"}, ToolContext(settings, confirm=no))
    assert res.is_error and kb.typed == [] and "Windows PowerShell" in asked[0]
    res = await registry.execute("press_keys", {"keys": "enter"}, ToolContext(settings, confirm=no))
    assert res.is_error and kb.combos == []
    res = await registry.execute("press_keys", {"keys": "ctrl+c"}, ToolContext(settings, confirm=no))
    assert not res.is_error and len(asked) == 2                     # copy is harmless: no question
    remote = await registry.execute("type_text", {"text": "x"}, ToolContext(settings, remote=True))
    assert remote.is_error and "blocked" in remote.content


def test_this_window(monkeypatch, settings):
    fake = use(monkeypatch, NOTEPAD)
    assert pc.window_control({"action": "minimize", "app": "this"}, ToolContext(settings)) == "Minimized notepad."
    assert fake.calls == [(5, "minimize")]


def test_nova_hud_is_never_the_active_window():
    assert pc.is_nova_window(pc.Win(1, "Nova", "msedge.exe"))
    assert not pc.is_nova_window(pc.Win(2, "Nova Scotia - Google Search - Chrome", "chrome.exe"))


@pytest.mark.parametrize("text,grid,expected", [
    ("Type hello there.", False, ("type_text", {"text": "hello there"})),
    ("type: Dear John, how are you?", False, ("type_text", {"text": "Dear John, how are you?"})),
    ("press enter", False, ("press_keys", {"keys": "enter"})),
    ("Press alt F4", False, ("press_keys", {"keys": "alt f4"})),
    ("press tab three times", False, ("press_keys", {"keys": "tab", "times": 3})),
    ("press 3", False, ("press_keys", {"keys": "3"})),
    ("press 3", True, ("mouse", {"action": "click", "cell": 3})),
    ("copy that", False, ("press_keys", {"keys": "ctrl+c"})),
    ("new tab", False, ("my_browser", {"action": "new_tab"})),
    ("close this tab", False, ("my_browser", {"action": "close_tab"})),
    ("go back", False, ("press_keys", {"keys": "alt+left"})),
    ("go back", True, ("mouse_grid", {"action": "back"})),
    ("refresh the page", False, ("press_keys", {"keys": "f5"})),
    ("minimize this", False, ("window", {"action": "minimize", "app": "this"})),
    ("close this window", False, ("window", {"action": "close", "app": "this"})),
    ("snap this to the left", False, ("press_keys", {"keys": "win+left"})),
    ("move this window to the other screen", False, ("press_keys", {"keys": "win+shift+right"})),
    ("zoom in", False, ("press_keys", {"keys": "ctrl+plus"})),
    ("zoom in on 14", False, ("mouse_grid", {"action": "zoom", "cell": 14})),
    ("press play", False, ("video", {"actions": ["play"]})),
    ("Pause", False, ("media", {"action": "pause"})),
])
def test_phrases(text, grid, expected):
    assert match_intent(text, grid_visible=grid) == expected


async def test_typing_while_novas_window_is_in_front_goes_to_your_window(settings, registry, kb, monkeypatch):
    class Wins(FakeWindows):
        def foreground(self):
            return 1                                   # the Nova window (just clicked)

        def focus(self, hwnd):
            self.calls.append((hwnd, "focus"))
            return True
    wins = Wins(NOTEPAD)
    monkeypatch.setattr(pc, "WINDOWS", wins)
    res = await registry.execute("type_text", {"text": "hello"}, ToolContext(settings))
    assert not res.is_error and wins.calls == [(5, "focus")] and kb.typed == ["hello"]



# --- owner: "he says I opened a new tab and did this, but he literally did nothing" ---------------
class TitleWindows(FakeWindows):
    """The window's title changes when a shortcut really does something."""

    def __init__(self, active, works=True):
        super().__init__(active)
        self.works = works

    def foreground(self):
        return self._active.hwnd

    def focus(self, hwnd):
        return True


async def test_a_new_tab_is_checked_and_named(settings, registry, kb, monkeypatch):
    firefox = pc.Win(7, "YouTube — Mozilla Firefox", "firefox.exe")
    wins = TitleWindows(firefox)
    monkeypatch.setattr(pc, "WINDOWS", wins)
    monkeypatch.setattr(K, "CHECK_S", 0)

    def combo(vks):
        kb.combos.append(vks)
        wins._active = pc.Win(7, "New Tab — Mozilla Firefox", "firefox.exe")
    monkeypatch.setattr(kb, "combo", combo)
    res = await registry.execute("press_keys", {"keys": "ctrl+t"}, ToolContext(settings))
    assert res.content == "Opened a new tab in Firefox."


async def test_a_shortcut_that_did_nothing_is_not_reported_as_done(settings, registry, kb, monkeypatch):
    monkeypatch.setattr(pc, "WINDOWS", TitleWindows(pc.Win(7, "YouTube — Mozilla Firefox", "firefox.exe")))
    monkeypatch.setattr(K, "CHECK_S", 0)
    res = await registry.execute("press_keys", {"keys": "ctrl+t"}, ToolContext(settings))
    assert res.is_error and "no new tab appeared" in res.content and "Firefox" in res.content


class PastingKeyboard(FakeKeyboard):
    def __init__(self):
        super().__init__()
        self.pasted = []

    def paste(self, text):
        self.pasted.append(text)


async def test_longer_text_is_pasted_not_typed_key_by_key(settings, registry, monkeypatch):
    """Owner's case: typing a long answer into Notepad came out as "cccccccccc" and rows of "?";
    pasting came out right. More than a few words (or several lines) is pasted."""
    fake = PastingKeyboard()
    monkeypatch.setattr(K, "KEYBOARD", fake)
    use(monkeypatch, NOTEPAD)
    answer = "In a bicep curl, the elbow is the fulcrum and the forearm is the lever."
    assert not (await registry.execute("type_text", {"text": answer}, ToolContext(settings))).is_error
    assert not (await registry.execute("type_text", {"text": "one\ntwo"}, ToolContext(settings))).is_error
    assert not (await registry.execute("type_text", {"text": "hello there"}, ToolContext(settings))).is_error
    assert fake.pasted == [answer, "one\ntwo"] and fake.typed == ["hello there"]
