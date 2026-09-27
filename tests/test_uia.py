"""Click by name and 'show numbers' (UI Automation faked), plus dictation mode."""

import pytest

from assistant.brain.intents import match_intent
from assistant.tools import grid as G, keyboard as K, pc, uia as U
from assistant.tools.registry import ToolContext, ToolError
from tests.test_barge import make
from tests.test_grid import FakeMouse, FakeOverlay
from tests.test_keyboard import FakeKeyboard
from tests.voice_helpers import FakeTTS

R = G.Region
YOUTUBE = [
    U.Element("Search", "box", R(700, 95, 500, 40)),
    U.Element("Search", "button", R(1210, 95, 50, 40)),
    U.Element("Play (k)", "button", R(90, 700, 40, 40)),
    U.Element("Subscribe", "button", R(600, 800, 110, 36)),
    U.Element("Subscribed", "button", R(1600, 300, 110, 36)),
    U.Element("Mute (m)", "button", R(140, 700, 40, 40)),
    U.Element("", "button", R(10, 10, 30, 30)),                      # icon without a name
]


class FakeUIA:
    def __init__(self, elements):
        self.els = elements

    def elements(self, hwnd):
        return self.els


@pytest.fixture
def rig(settings, monkeypatch):
    monkeypatch.setattr(U, "UIA", FakeUIA(YOUTUBE))
    monkeypatch.setattr(pc, "active_window", lambda: pc.Win(3, "YouTube — Mozilla Firefox", "firefox.exe"))
    c = G.GridController(mouse=FakeMouse(), overlay=FakeOverlay())
    c.mouse.virtual = lambda: R(0, 0, 2560, 1600)
    c.overlay.show_labels = lambda labels, screen: setattr(c.overlay, "labels", labels)
    return c, ToolContext(settings, services={"grid": c})


def test_click_by_name(rig):
    c, ctx = rig
    assert U.click_element({"name": "subscribe"}, ctx) == "Clicked Subscribe."      # not "Subscribed"
    assert c.mouse.log[:2] == [("move", 655, 818), ("click", "left", False)]
    assert U.click_element({"name": "the play button"}, ctx) == "Clicked Play (k)."
    assert U.click_element({"name": "on the search box"}, ctx) == "Clicked Search."
    assert c.mouse.log[-2] == ("move", 950, 115)                                    # the box, not the button
    assert not c.visible


def test_ambiguous_name_shows_numbers(rig):
    c, ctx = rig
    assert U.click_element({"name": "search"}, ctx) == "I see 2 of those. Say 'click' and a number."
    assert c.labels is not None and len(c.labels) == 2 and c.mouse.log == []
    G.mouse({"action": "click", "cell": 2}, ctx)
    assert c.mouse.log[0] == ("move", 1235, 115)


def test_not_found_and_numbers(rig, monkeypatch):
    c, ctx = rig
    with pytest.raises(ToolError, match="show numbers"):
        U.click_element({"name": "download"}, ctx)
    out = U.show_numbers({}, ctx)
    assert out == "Numbers on: 7 things. Say 'click' and a number."
    assert c.labels[1] == R(10, 10, 30, 30)                                         # reading order
    with pytest.raises(ToolError, match="click' and the number"):
        G.mouse_grid({"action": "zoom", "cell": 1}, ctx)
    monkeypatch.setattr(U, "UIA", FakeUIA([]))
    with pytest.raises(ToolError, match="grid"):
        U.show_numbers({}, ctx)


def test_split_kind():
    assert U.split_kind("the search box") == ("search", "box")
    assert U.split_kind("Sign in button") == ("sign in", "button")
    assert U.split_kind("settings") == ("settings", None)


@pytest.mark.parametrize("text,grid,labels,expected", [
    ("show numbers", False, False, ("show_numbers", {})),
    ("what can I click", False, False, ("show_numbers", {})),
    ("7", False, True, ("mouse", {"action": "click", "cell": 7})),
    ("hide numbers", False, True, ("mouse_grid", {"action": "hide"})),
    ("click subscribe", False, False, ("click_element", {"name": "subscribe"})),
    ("double click the readme file", False, False,
     ("click_element", {"name": "the readme file", "action": "double_click"})),
    ("press the sign in button", False, False, ("click_element", {"name": "the sign in button"})),
    ("select all", False, False, ("press_keys", {"keys": "ctrl+a"})),
    ("click it", True, False, ("mouse", {"action": "click"})),
    ("start dictation", False, False, ("dictation", {"on": True})),
    ("stop dictation", False, False, ("dictation", {"on": False})),
])
def test_phrases(text, grid, labels, expected):
    assert match_intent(text, grid_visible=grid, labels=labels) == expected


# --- dictation --------------------------------------------------------------------------------------
async def test_dictation_types_everything_until_stopped(settings, registry, monkeypatch):
    kb = FakeKeyboard()
    monkeypatch.setattr(K, "KEYBOARD", kb)
    monkeypatch.setattr(pc, "WINDOWS", type("W", (), {"active": lambda self: pc.Win(5, "Notepad", "notepad.exe"),
                                                      "list": lambda self: []})())
    loop, events = make(settings, registry, [], [
        ("say", "Dear John", 10), ("quiet", 20),
        ("say", "new line", 10), ("quiet", 20),
        ("say", "I hope you are well", 10), ("quiet", 20),
        ("say", "scratch that", 10), ("quiet", 20),
        ("say", "stop dictation", 10), ("quiet", 20),
        ("until", lambda l: not l.dictation and not l.busy),
        ("say", "this is not typed", 10), ("quiet", 20)], tts=FakeTTS())
    loop.set_dictation(True)
    await loop.run()
    assert kb.typed == ["Dear John ", "I hope you are well "]
    assert kb.combos.count([0x0D]) == 1                                  # new line
    assert kb.combos.count([0x08]) == len("I hope you are well ")        # scratch that
    assert "Dictation off." in loop.tts.spoken
    assert loop.brain.client.messages.calls == []                        # nothing went to the model


async def test_dictation_times_out(settings, registry, monkeypatch):
    kb = FakeKeyboard()
    monkeypatch.setattr(K, "KEYBOARD", kb)
    settings.voice.dictation_timeout_s = 0
    loop, events = make(settings, registry, [], [("say", "people talking in my game", 10), ("quiet", 20)],
                        tts=FakeTTS())
    loop.set_dictation(True)
    await loop.run()
    assert kb.typed == [] and not loop.dictation
