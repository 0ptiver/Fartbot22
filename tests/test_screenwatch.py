"""Seamless PC control (owner's pick: "reading errors, managing apps, or helping with tasks without
needing a mouse"): Nova notices error boxes and frozen apps himself, and arranges windows on two screens."""

import pytest

from assistant.core.conversation import Conversation
from assistant.core.screenwatch import ScreenWatch
from assistant.tools import pc
from assistant.tools.registry import ToolError
from tests.test_agent import FakeAgent, said, with_agent
from tests.test_local_brain import collect, make


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class FakeDesk:
    """Two screens (1920x1040 work area each, side by side), windows that can move, dialogs, hangs."""

    def __init__(self):
        self.wins = {1: pc.Win(1, "Mozilla Firefox", "firefox.exe"), 2: pc.Win(2, "Discord", "Discord.exe"),
                     3: pc.Win(3, "Spotify Premium", "Spotify.exe")}
        self.rects = {1: (100, 100, 1200, 800), 2: (300, 200, 1000, 700), 3: (2200, 100, 1000, 800)}
        self.popups: list[pc.Win] = []
        self.frozen: set[int] = set()
        self.stuck: set[int] = set()          # windows that refuse to move
        self.states = {1: "normal", 2: "normal", 3: "maximized"}
        self.fullscreen = False

    def list(self):
        return list(self.wins.values())

    def active(self):
        return self.wins[1]

    def foreground(self):
        return 1

    def dialogs(self):
        return list(self.popups)

    def hung(self, hwnd):
        return hwnd in self.frozen

    def rect(self, hwnd):
        return self.rects[hwnd]

    def work_areas(self):
        return [(0, 0, 1920, 1040), (1920, 0, 1920, 1040)]

    def move(self, hwnd, x, y, w, h):
        if hwnd not in self.stuck:
            self.rects[hwnd] = (x, y, w, h)
            self.states[hwnd] = "normal"

    def state(self, hwnd):
        return self.states[hwnd]

    def show(self, hwnd, how):
        if how == "maximize":
            self.states[hwnd] = "maximized"

    def is_fullscreen(self, hwnd):
        return self.fullscreen


@pytest.fixture
def desk(monkeypatch):
    d = FakeDesk()
    monkeypatch.setattr(pc, "WINDOWS", d)
    return d


def watcher(said_list):
    async def notify(text):
        said_list.append(text)
    texts = {9: "Steam: Disk write error. The files could not be saved.", 10: "Save changes to notes.txt?"}
    clock = Clock()
    return ScreenWatch(notify, read_text=lambda hwnd: texts.get(hwnd, ""), clock=clock), clock


# --- noticing trouble ------------------------------------------------------------------------------
async def test_a_new_error_box_is_read_out_once(desk):
    out = []
    w, _ = watcher(out)
    await w.check()                                   # first look: what was already open is ignored
    desk.popups = [pc.Win(9, "Steam - Error", "steam.exe"), pc.Win(10, "Notepad", "notepad.exe")]
    await w.check()
    await w.check()
    assert out == ["Steam says: Steam: Disk write error. The files could not be saved. Say 'fix it' and I'll sort it out."]
    assert w.recent().kind == "error" and "Disk write error" in w.recent().describe()   # "save changes?" isn't an error


async def test_a_frozen_app_is_noticed_after_a_few_checks(desk):
    out = []
    w, _ = watcher(out)
    await w.check()
    desk.frozen = {2}
    for _ in range(4):
        await w.check()
    assert out == ["Discord isn't responding. Say 'close it' to end it, or 'ignore it'."]


async def test_alerts_stay_quiet_during_a_fullscreen_game(desk):
    out = []
    w, _ = watcher(out)
    await w.check()
    desk.fullscreen = True
    desk.popups = [pc.Win(9, "Steam - Error", "steam.exe")]
    await w.check()
    assert out == [] and w.recent() is not None        # kept, in case he asks


# --- replying to an alert ------------------------------------------------------------------------
async def test_fix_it_sends_claude_with_the_error_text(local_settings, ctx, desk):
    out = []
    w, _ = watcher(out)
    await w.check()
    desk.popups = [pc.Win(9, "Steam - Error", "steam.exe")]
    await w.check()
    ctx.services["screenwatch"] = w
    brain, fake = make(local_settings, [])
    agent = with_agent(brain, FakeAgent(say="Freed up space and retried the download", steps=[]))
    events = await collect(brain, Conversation(), "fix it", ctx)
    assert agent.calls and "Disk write error" in agent.calls[0][0] and "Freed up space" in said(events)
    assert w.recent() is None


async def test_what_does_it_say_and_ignore_it(local_settings, ctx, desk):
    out = []
    w, _ = watcher(out)
    await w.check()
    desk.popups = [pc.Win(9, "Steam - Error", "steam.exe")]
    await w.check()
    ctx.services["screenwatch"] = w
    brain, fake = make(local_settings, [])
    assert "Disk write error" in said(await collect(brain, Conversation(), "what does it say?", ctx))
    assert said(await collect(brain, Conversation(), "ignore it", ctx)) == "Very good, sir."
    assert w.recent() is None and not fake.requests


async def test_close_it_ends_the_frozen_app_not_the_window_in_front(local_settings, ctx, desk, monkeypatch):
    out = []
    w, _ = watcher(out)
    await w.check()
    desk.frozen = {2}
    for _ in range(3):
        await w.check()
    ctx.services["screenwatch"] = w
    ended = []
    monkeypatch.setattr(pc, "end_processes", lambda exe: ended.append(exe) or 1)
    brain, fake = make(local_settings, [])
    events = await collect(brain, Conversation(), "close it", ctx)
    assert ended == ["Discord.exe"] and "Discord" in said(events)


async def test_fix_this_error_with_nothing_spotted_still_looks(local_settings, ctx, desk):
    brain, fake = make(local_settings, [])
    agent = with_agent(brain, FakeAgent(say="Done", steps=[]))
    await collect(brain, Conversation(), "can you fix this error", ctx)
    assert agent.calls and "error or problem showing on Oliver's screen" in agent.calls[0][0]


# --- arranging windows -----------------------------------------------------------------------------
def test_snap_left_and_right_on_the_screen_its_on(desk):
    assert pc.window_control({"action": "snap_left", "app": "firefox"}, None) == "Firefox is on the left half."
    assert desk.rects[1] == (0, 0, 960, 1040)
    assert pc.window_control({"action": "snap_right", "app": "spotify"}, None) == "Spotify is on the right half."
    assert desk.rects[3] == (1920 + 960, 0, 960, 1040)            # Spotify was on screen 2


def test_move_to_the_other_screen_keeps_it_maximised(desk):
    assert pc.window_control({"action": "other_screen", "app": "spotify"}, None) == "Moved Spotify to screen 1."
    x, y, w, h = desk.rects[3]
    assert x < 1920 and desk.states[3] == "maximized"


def test_side_by_side(desk):
    out = pc.window_control({"action": "side_by_side", "app": "spotify", "other": "discord"}, None)
    assert out == "Spotify on the left, Discord on the right."
    assert desk.rects[3][0] == 1920 and desk.rects[2][0] == 1920 + 960   # both on Spotify's screen


def test_a_window_that_wont_move_is_reported_not_claimed(desk):
    desk.stuck = {1}
    with pytest.raises(ToolError, match="stayed put"):
        pc.window_control({"action": "snap_left", "app": "firefox"}, None)


@pytest.mark.parametrize("said_,intent", [
    ("snap firefox to the left", ("window", {"action": "snap_left", "app": "firefox"})),
    ("move spotify to my other screen", ("window", {"action": "other_screen", "app": "spotify"})),
    ("put spotify and discord side by side", ("window", {"action": "side_by_side", "app": "spotify", "other": "discord"})),
])
def test_arranging_is_said_directly(said_, intent):
    from assistant.brain.intents import match_intent
    assert match_intent(said_) == intent


def test_moving_the_mouse_is_not_a_window():
    from assistant.brain.intents import match_intent
    assert match_intent("move the mouse left") is None or match_intent("move the mouse left")[0] != "window"

