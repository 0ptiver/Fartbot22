"""Voice mouse grid: cell maths, zoom/back, clicks, fast-path phrases, no-name commands."""

import pytest

from assistant.brain.intents import match_intent
from assistant.tools import grid as G
from assistant.tools.registry import ToolContext, ToolError
from tests.fakes import text_msg
from tests.test_barge import make
from tests.voice_helpers import FakeTTS


class FakeMouse:
    def __init__(self):
        self.log = []

    def screen(self):
        return G.Region(0, 0, 2560, 1600)

    def move(self, x, y):
        self.log.append(("move", x, y))

    def click(self, button="left", double=False):
        self.log.append(("click", button, double))

    def scroll(self, notches):
        self.log.append(("scroll", notches))

    def drag(self, *xy):
        self.log.append(("drag", *xy))


class FakeOverlay:
    def __init__(self):
        self.shown = None

    def show(self, grid):
        self.shown = grid

    def hide(self):
        self.shown = None


@pytest.fixture
def ctl(settings):
    c = G.GridController(mouse=FakeMouse(), overlay=FakeOverlay())
    ctx = ToolContext(settings, services={"grid": c})
    return c, ctx


def test_cells_cover_the_screen_exactly():
    g = G.Grid(G.Region(0, 0, 2560, 1600), 10, 6)
    assert g.count == 60
    assert g.cell(1) == G.Region(0, 0, 256, 267) and g.center(1) == (128, 133)
    last = g.cell(60)
    assert last.x + last.w == 2560 and last.y + last.h == 1600
    assert sum(g.cell(n).w for n in range(1, 11)) == 2560        # no gaps between columns
    with pytest.raises(ToolError, match="1 to 60"):
        g.cell(61)


def test_show_zoom_click(ctl):
    c, ctx = ctl
    assert "Grid on" in G.mouse_grid({"action": "show"}, ctx) and c.visible
    G.mouse_grid({"action": "zoom", "cell": 12}, ctx)              # row 2, column 2
    assert c.grid.region == G.Region(256, 267, 256, 266) and c.grid.count == 9
    assert c.overlay.shown is c.grid
    assert G.mouse({"action": "click", "cell": 5}, ctx) == "Clicked."
    assert c.mouse.log == [("move", 384, 400), ("click", "left", False)]
    assert not c.visible and c.overlay.shown is None                # clicking hides the grid


def test_back_zoom_limits_and_other_actions(ctl):
    c, ctx = ctl
    with pytest.raises(ToolError, match="isn't showing"):
        G.mouse({"action": "click", "cell": 3}, ctx)
    G.mouse_grid({"action": "show"}, ctx)
    G.mouse_grid({"action": "zoom", "cell": 1}, ctx)
    assert G.mouse_grid({"action": "back"}, ctx) == "Zoomed out." and c.grid.count == 60
    for _ in range(4):
        try:
            G.mouse_grid({"action": "zoom", "cell": 5}, ctx)
        except ToolError as e:
            assert "as close as it goes" in str(e)
            break
    else:
        raise AssertionError("zoom never stopped")
    G.mouse({"action": "scroll_down", "amount": 50}, ctx)
    assert c.mouse.log[-1] == ("scroll", -30)                       # capped
    G.mouse({"action": "double_click"}, ctx)                        # where the mouse is
    assert c.mouse.log[-1] == ("click", "left", True)
    G.mouse_grid({"action": "show"}, ctx)
    G.mouse({"action": "drag", "cell": 1, "to": 60}, ctx)
    assert c.mouse.log[-1][0] == "drag" and not c.visible
    assert G.mouse_grid({"action": "hide"}, ctx) == "Grid off."


async def test_mouse_is_blocked_remotely(settings, registry):
    res = await registry.execute("mouse", {"action": "click"}, ToolContext(settings, remote=True))
    assert res.is_error and "blocked" in res.content


def test_real_backend_refuses_off_windows(monkeypatch):
    monkeypatch.setattr(G, "IS_WINDOWS", False)
    with pytest.raises(ToolError, match="Windows"):
        G.MouseBackend().screen()


@pytest.mark.parametrize("text,visible,expected", [
    ("Show the grid.", False, ("mouse_grid", {"action": "show"})),
    ("Mouse grid", False, ("mouse_grid", {"action": "show"})),
    ("Click on fourteen", True, ("mouse", {"action": "click", "cell": 14})),
    ("double-click 7", True, ("mouse", {"action": "double_click", "cell": 7})),
    ("right click", False, ("mouse", {"action": "right_click"})),
    ("zoom in on 22", True, ("mouse_grid", {"action": "zoom", "cell": 22})),
    ("22", True, ("mouse_grid", {"action": "zoom", "cell": 22})),
    ("22", False, None),                                        # a bare number means nothing without the grid
    ("cancel", True, ("mouse_grid", {"action": "hide"})),
    ("cancel", False, None),
    ("scroll down a lot", False, ("mouse", {"action": "scroll_down", "amount": 10})),
    ("drag 5 to 12", True, ("mouse", {"action": "drag", "cell": 5, "to": 12})),
    ("click the play button", False, None),                     # the model handles descriptions
    # Owner's case: "Can you now hit play on the video on my screen?" got "I can't see your screen".
    ("Can you now hit play on the video on my screen?", False, ("media_key", {"action": "play_pause"})),
    ("Pause the video", False, ("media_key", {"action": "play_pause"})),
])
def test_phrases(text, visible, expected):
    assert match_intent(text, grid_visible=visible) == expected


async def test_grid_commands_need_no_name_while_showing(settings, registry):
    loop, events = make(settings, registry, [text_msg("Noon, sir.")], [
        ("say", "click fourteen", 10), ("quiet", 20),                 # grid hidden: ignored
        ("until", lambda l: not l.busy),
        ("say", "zoom 14", 10), ("quiet", 20),                        # grid showing: accepted
        ("until", lambda l: l.turns_done >= 2 and not l.busy)], tts=FakeTTS())
    c = G.GridController(mouse=FakeMouse(), overlay=FakeOverlay())
    loop.ctx.services["grid"] = c
    orig = loop._check_wake

    async def check(text, lat):
        if "zoom" in text:                                        # the grid appears before step 2
            c.grid = G.Grid(G.Region(0, 0, 2560, 1600), 10, 6)
        return await orig(text, lat)
    loop._check_wake = check
    await loop.run()
    assert any(e["type"] == "ignored" and "click" in e["text"] for e in events)
    assert any(e["type"] == "transcript" and e["text"] == "zoom 14" for e in events)
