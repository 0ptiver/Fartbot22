"""Teach by showing: events -> steps, the lesson flow, saving, and replay (Windows faked)."""

import pytest

from assistant.brain.intents import match_intent
from assistant.core.conversation import Conversation
from assistant.tools import grid as G, keyboard as K, pc, routines, teach as T, uia as U
from assistant.tools.registry import ToolContext
from tests.test_grid import FakeMouse, FakeOverlay
from tests.test_keyboard import FakeKeyboard

STEAM = {"window": {"process": "steam.exe", "title": "Steam"}, "element": {"name": "Library", "kind": "tab"},
         "rel": [0.2, 0.1]}
NOVA = {"window": {"process": "msedge.exe", "title": "Nova"}}


def click(t, x=300, y=100, target=STEAM, button="left"):
    return [T.Event("down", t, button=button, x=x, y=y, target=target), T.Event("up", t + 0.08, button=button, x=x, y=y)]


def key(t, vk, mods=(), password=False):
    return T.Event("key", t, vk=vk, mods=tuple(mods), password=password)


def test_events_become_steps():
    events = [T.Event("tool", 0.0, tool="open_app", args={"name": "steam"}),
              *click(3.0),
              *click(3.3, target=NOVA),                                       # Nova's own window: ignored
              key(4.0, ord("G")), key(4.1, ord("T")), key(4.2, ord("A"), ["shift"]), key(4.3, 0x0D),
              key(5.0, ord("C"), ["ctrl"]),
              key(6.0, ord("H"), password=True), key(6.1, ord("I"), password=True),
              *click(7.0, x=500, y=500, target=None)]                         # nothing known under it: skipped
    steps, skipped = T.build_steps(events)
    assert skipped                                                             # the password typing
    assert [T.describe_step(s) for s in steps] == [
        "open app (steam)", "wait 3 s", 'click "Library" in steam', "wait 0.9 s", "type gtA[Enter]",
        "wait 0.7 s", "type [Ctrl+C]"]


def test_double_click_and_drag():
    steps, _ = T.build_steps([*click(1.0), *click(1.25)])
    assert len(steps) == 1 and steps[0]["args"]["double"]
    steps, _ = T.build_steps([T.Event("down", 1.0, button="left", x=10, y=10, target=STEAM),
                              T.Event("up", 1.5, button="left", x=200, y=10)])
    assert steps[0]["args"]["drag_to"] == [200, 10] and T.describe_step(steps[0]).startswith("drag")


class FakePoller:
    def __init__(self):
        self.emit, self.running = None, False

    def start(self, emit):
        self.emit, self.running = emit, True

    def stop(self):
        self.running = False


def test_lesson_flow_saves_a_routine(settings, registry):
    t = T.Teacher(registry, FakePoller(), clock=lambda: 0.0)
    assert t.start().startswith("Watching")
    for ev in [*click(1.0), key(2.0, ord("K"))]:
        t.poller.emit(ev)
    msg = t.stop()
    assert msg.startswith("Got it: 2 steps") and "What should I call it" in msg and t.awaiting_name
    assert t.save("Morning setup.") == "Saved. Say 'Nova, Morning setup' and I'll do it."
    learned = T.load_learned()
    assert learned["morning_setup"]["phrases"] == ["morning setup"]
    assert "morning_setup" in routines.active(settings)
    assert routines.match_routine("morning setup", settings) == ("run_routine", {"name": "morning_setup"})
    assert T.delete_learned("the morning setup routine") == "Forgotten: morning setup."
    assert T.load_learned() == {}


def test_nothing_done_and_cancel(registry):
    t = T.Teacher(registry, FakePoller())
    t.start()
    assert "nothing to save" in t.stop()
    t.start()
    t.poller.emit(click(1.0)[0])
    assert t.cancel() == "Forgotten; nothing saved." and not t.recording


def test_nova_actions_during_a_lesson_are_recorded(registry):
    t = T.Teacher(registry, FakePoller(), clock=lambda: 1.0)
    t.start()
    t._on_tool("open_app", {"name": "discord"}, True)
    t._on_tool("mouse", {"action": "click"}, True)                            # raw input covers clicks
    t._on_tool("open_website", {"site": "x"}, False)                          # failed: not repeated
    t.stop()
    assert [s.get("tool") for s in t.pending] == ["open_app"]


class FakeWins:
    def __init__(self, wins):
        self.wins, self.calls = wins, []

    def list(self):
        return self.wins

    def show(self, hwnd, how):
        self.calls.append((hwnd, how))

    def rect(self, hwnd):
        return (100, 50, 1000, 800)

    def active(self):
        return self.wins[0] if self.wins else None


@pytest.fixture
def replay(settings, monkeypatch):
    wins = FakeWins([pc.Win(9, "Steam", "steam.exe")])
    monkeypatch.setattr(pc, "WINDOWS", wins)
    c = G.GridController(mouse=FakeMouse(), overlay=FakeOverlay())
    monkeypatch.setattr(T.asyncio, "sleep", _fast_sleep)
    return wins, c, ToolContext(settings, services={"grid": c})


async def _fast_sleep(*a):
    return None


async def test_replay_click_finds_the_button_by_name(replay, monkeypatch):
    wins, c, ctx = replay

    class UIA:
        def elements(self, hwnd):
            return [U.Element("Store", "tab", G.Region(150, 70, 80, 30)),
                    U.Element("Library", "tab", G.Region(700, 400, 80, 30))]      # the window moved things
    monkeypatch.setattr(U, "UIA", UIA())
    args = {"button": "left", "double": False, **STEAM, "abs": [300, 130]}
    assert await T.replay_click(args, ctx) == "Clicked."
    assert wins.calls[0] == (9, "focus") and c.mouse.log[:2] == [("move", 740, 415), ("click", "left", False)]


async def test_replay_click_falls_back_to_the_spot_in_the_window(replay, monkeypatch):
    wins, c, ctx = replay
    monkeypatch.setattr(U, "UIA", type("U", (), {"elements": lambda s, h: []})())
    monkeypatch.setattr(T.time, "monotonic", iter(range(0, 1000, 5)).__next__)   # time runs out at once
    await T.replay_click({"button": "left", **STEAM, "abs": [300, 130]}, ctx)
    assert c.mouse.log[0] == ("move", 100 + 200, 50 + 80)                         # rel 0.2, 0.1 of the window
    wins.wins = []
    T.WINDOW_WAIT_S = 0
    try:
        with pytest.raises(Exception, match="isn't open"):
            await T.replay_click({"button": "left", **STEAM, "abs": [1, 1]}, ctx)
    finally:
        T.WINDOW_WAIT_S = 8.0


async def test_replay_keys_and_terminal_guard(settings, registry, monkeypatch):
    kb = FakeKeyboard()
    monkeypatch.setattr(K, "KEYBOARD", kb)
    monkeypatch.setattr(pc, "WINDOWS", FakeWins([pc.Win(5, "Notepad", "notepad.exe")]))
    monkeypatch.setattr(T.asyncio, "sleep", _fast_sleep)
    keys = [[ord("A"), ["shift"]], [0x0D, []], [ord("C"), ["ctrl"]]]
    res = await registry.execute("replay_keys", {"keys": keys, "shows": "A[Enter][Ctrl+C]"}, ToolContext(settings))
    assert not res.is_error and kb.combos == [[0x10, ord("A")], [0x0D], [0x11, ord("C")]]
    monkeypatch.setattr(pc, "WINDOWS", FakeWins([pc.Win(6, "Windows PowerShell", "WindowsTerminal.exe")]))

    async def no(*a):
        return False
    res = await registry.execute("replay_keys", {"keys": keys}, ToolContext(settings, confirm=no))
    assert res.is_error and len(kb.combos) == 3


async def test_whole_lesson_by_voice(settings, registry, monkeypatch):
    """'watch what I do' ... 'done' ... 'morning setup' ... later 'morning setup' replays it."""
    from tests.test_local_brain import collect, make
    settings.brain.backend = "local"
    brain, fake = make(settings, [])
    teacher = T.Teacher(brain.registry, FakePoller(), clock=lambda: 0.0)
    ctx = ToolContext(settings, services={"teacher": teacher})
    conv = Conversation()
    from assistant.brain.llm import TextDelta
    said = lambda evs: "".join(e.text for e in evs if isinstance(e, TextDelta))  # noqa: E731
    assert said(await collect(brain, conv, "Watch what I do", ctx)).startswith("Watching")
    teacher.poller.emit(T.Event("tool", 0.0, tool="open_app", args={"name": "steam"}))
    assert "What should I call it" in said(await collect(brain, conv, "Done.", ctx))
    assert said(await collect(brain, conv, "Morning setup", ctx)).startswith("Saved")
    opened = []
    brain.registry.get("open_app").handler = lambda a, c: opened.append(a["name"]) or "Opened Steam."
    assert said(await collect(brain, conv, "Morning setup", ctx)) == "Done."
    assert opened == ["steam"] and fake.requests == []                         # no model call at all


@pytest.mark.parametrize("text,expected", [
    ("watch what I do", {"action": "start"}), ("let me show you something", {"action": "start"}),
    ("learn this", {"action": "start"}), ("call it evening setup", {"action": "save", "name": "evening setup"}),
    ("forget the evening setup routine", {"action": "delete", "name": "evening setup"}),
    ("what have you learned", {"action": "list"}),
])
def test_phrases(text, expected):
    assert match_intent(text) == ("teach", expected)


def test_done_only_counts_during_a_lesson(registry):
    assert match_intent("done") is None
    t = T.Teacher(registry, FakePoller())
    t.recording = True
    assert T.teach_intent("that's it", t) == ("teach", {"action": "stop"})
    t.recording, t.awaiting_name = False, True
    assert T.teach_intent("gaming stuff", t) == ("teach", {"action": "save", "name": "gaming stuff"})
    assert T.teach_intent("never mind", t) == ("teach", {"action": "cancel"})
