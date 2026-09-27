"""Browser video control: finds the video via Windows' media list, exact play/pause, keys
go to the right browser window."""

import pytest

from assistant.brain.intents import match_intent
from assistant.tools import music, pc, video as V
from assistant.tools.registry import ToolContext, ToolError


class FakeMedia:
    """Like Windows' media list: a command changes the player's status, unless the player
    ignores it (works=False: "accepted", nothing happens; the owner's case)."""

    def __init__(self, items, works=True):
        self.items, self.commands, self.works = items, [], works

    async def list(self):
        return [V.Media(m.id, m.app, m.title, m.artist, m.status) for m in self.items]

    async def command(self, media, action):
        self.commands.append((media.title, action))
        if self.works and action in ("play", "pause"):
            for m in self.items:
                if m.title == media.title:
                    m.status = "playing" if action == "play" else "paused"
        return True


class FakeWindows:
    """Pressing F (or clicking the player's button) makes the video full screen; Esc undoes it."""

    def __init__(self, wins, focus_ok=True, f_works=True):
        self.wins, self.calls, self.full = wins, [], False
        self.focus_ok, self.f_works = focus_ok, f_works
        self.on_k = None

    def list(self):
        return self.wins

    def show(self, hwnd, how):
        self.calls.append(("show", hwnd, how))

    def focus(self, hwnd):
        self.calls.append(("show", hwnd, "focus"))
        return self.focus_ok

    def is_fullscreen(self, hwnd):
        return self.full

    def press(self, vk):
        self.calls.append(("press", vk))
        if vk == pc.KEYS["k"] and self.on_k:
            self.on_k()
        if vk == pc.KEYS["f"] and self.f_works:
            self.full = not self.full
        if vk == pc.KEYS["escape"]:
            self.full = False


def yt():
    return V.Media(0, "308046B0AF4A39CB", "Streamer Gets STALKED by Lil Vape!", "MoreCrown", "paused")


def spotify():
    return V.Media(1, "Spotify.exe", "My Way", "Kanye West", "playing")


YT, SPOTIFY = yt(), spotify()
WINDOWS = [pc.Win(1, "Nova", "msedge.exe"),                                  # the HUD: never this
           pc.Win(2, "Windows PowerShell", "WindowsTerminal.exe"),
           pc.Win(3, "(1) Streamer Gets STALKED by Lil Vape! - YouTube — Mozilla Firefox", "firefox.exe")]


@pytest.fixture
def wins(monkeypatch):
    fake = FakeWindows(list(WINDOWS))
    monkeypatch.setattr(pc, "WINDOWS", fake)
    monkeypatch.setattr(V.asyncio, "sleep", _no_sleep)
    from assistant.tools import uia
    monkeypatch.setattr(uia, "UIA", type("NoUIA", (), {"elements": lambda s, h: []})())   # no buttons: keys
    return fake


async def _no_sleep(*a):
    return None


async def test_owners_case_fullscreen_then_play(settings, wins):
    """'Can you full screen the video and press play' got 'I don't know which video you mean'."""
    intent = match_intent("Can you full screen the video and press play")
    assert intent == ("video", {"actions": ["fullscreen", "play"]})
    media = FakeMedia([spotify(), yt()])
    out = await V.video(intent[1], ToolContext(settings), _media=media)
    assert out == "Full screen, playing: Streamer Gets STALKED by Lil Vape!."
    assert wins.calls[:2] == [("show", 3, "focus"), ("press", pc.KEYS["f"])]   # Firefox, not Nova
    assert media.commands == [("Streamer Gets STALKED by Lil Vape!", "play")]    # not Spotify


async def test_play_is_exact_not_a_toggle(settings, wins):
    playing = V.Media(0, "chrome.exe", "Lofi", "", "playing")
    media = FakeMedia([playing])
    out = await V.video({"actions": ["play"]}, ToolContext(settings), _media=media)
    assert out.startswith("Already playing") and media.commands == []            # didn't pause it


async def test_no_media_session_falls_back_to_media_key(settings, wins, monkeypatch):
    pressed = []
    monkeypatch.setattr(music, "press_media_key", pressed.append)
    out = await V.video({"actions": ["pause"]}, ToolContext(settings), _media=FakeMedia([SPOTIFY]))
    assert pressed == ["play_pause"] and "play/pause" in out
    with pytest.raises(ToolError, match="can't find a video"):
        await V.video({"actions": ["next"]}, ToolContext(settings), _media=FakeMedia([]))


async def test_window_found_by_site_when_title_differs(settings, wins):
    wins.wins = [pc.Win(1, "Nova", "msedge.exe"), pc.Win(7, "Twitch — Mozilla Firefox", "firefox.exe")]
    await V.video({"actions": ["fullscreen"]}, ToolContext(settings), _media=FakeMedia([]))
    assert wins.calls[0] == ("show", 7, "focus")
    wins.wins = [pc.Win(1, "Nova", "msedge.exe")]
    with pytest.raises(ToolError, match="browser window"):
        await V.video({"actions": ["fullscreen"]}, ToolContext(settings), _media=FakeMedia([]))


def test_pick_video_prefers_browser_playing():
    paused_tab = V.Media(0, "chrome.exe", "A", "", "paused")
    playing_tab = V.Media(1, "firefox.exe", "B", "", "playing")
    assert V.pick_video([SPOTIFY, paused_tab, playing_tab]) is playing_tab
    assert V.pick_video([SPOTIFY]) is None


@pytest.mark.parametrize("text,actions", [
    ("Can you now hit play on the video on my screen?", ["play"]),
    ("go full screen and play the video", ["fullscreen", "play"]),
    ("fullscreen it then play it", ["fullscreen", "play"]),
    ("exit full screen", ["exit_fullscreen"]),
    ("skip ahead on the video", ["forward"]),
    ("mute the video", ["mute"]),
    ("play the next video", ["next"]),
    ("hit play", ["play"]),
])
def test_video_phrases(text, actions):
    assert match_intent(text) == ("video", {"actions": actions})


@pytest.mark.parametrize("text", ["Pause", "next song", "play despacito", "what is this video about",
                                  "open youtube"])
def test_not_video_phrases(text):
    r = match_intent(text)
    assert r is None or r[0] != "video"


async def test_missing_winrt_piece_degrades_instead_of_crashing(settings, wins, monkeypatch):
    """Owner's case: 'No module named winrt.windows.foundation.collections' was spoken as the reply."""
    monkeypatch.setattr(V.sys, "platform", "win32")          # winrt isn't installed here: ImportError
    assert await V.MediaBackend().list() == []
    pressed = []
    monkeypatch.setattr(music, "press_media_key", pressed.append)
    out = await V.video({"actions": ["fullscreen", "play"]}, ToolContext(settings), _media=V.MediaBackend())
    assert out.startswith("Full screen") and pressed == ["play_pause"]


async def test_fullscreen_clicks_the_players_button_and_checks(settings, wins, monkeypatch):
    """Owner's case: 'full screen my video' did nothing (F went to the search box / wrong window)
    but Nova said it worked. Now the player's own button is clicked and the result checked."""
    from assistant.tools import grid as G, uia
    from tests.test_grid import FakeMouse, FakeOverlay
    wins.f_works = False                                           # F lands in the search box
    button = uia.Element("Full screen (f)", "button", G.Region(1500, 900, 40, 30))
    monkeypatch.setattr(uia, "UIA", type("U", (), {"elements": lambda s, h: [
        uia.Element("Exit full screen (f)", "button", G.Region(0, 0, 1, 1)) if wins.full else button]})())
    mouse = FakeMouse()
    real_click = mouse.click

    def click(button="left", double=False):
        real_click(button, double)
        wins.full = True                                           # the player goes full screen
    mouse.click = click
    ctx = ToolContext(settings, services={"grid": G.GridController(mouse=mouse, overlay=FakeOverlay())})
    out = await V.video({"actions": ["fullscreen"]}, ctx, _media=FakeMedia([YT]))
    assert out.startswith("Full screen") and mouse.log[0] == ("move", 1520, 915)
    out = await V.video({"actions": ["fullscreen"]}, ctx, _media=FakeMedia([YT]))
    assert out.startswith("Already full screen")
    out = await V.video({"actions": ["exit_fullscreen"]}, ctx, _media=FakeMedia([YT]))
    assert out.startswith("Out of full screen") and not wins.full


async def test_fullscreen_that_fails_says_so(settings, wins):
    wins.f_works = False                                           # no button, F does nothing
    with pytest.raises(ToolError, match="didn't go full screen"):
        await V.video({"actions": ["fullscreen"]}, ToolContext(settings), _media=FakeMedia([YT]))


async def test_window_that_wont_come_forward_says_so(settings, wins):
    wins.focus_ok = False
    with pytest.raises(ToolError, match="wouldn't let me switch"):
        await V.video({"actions": ["fullscreen"]}, ToolContext(settings), _media=FakeMedia([YT]))
    assert not any(c[0] == "press" for c in wins.calls)            # never presses keys blind



async def test_play_that_doesnt_happen_is_not_reported_as_done(settings, wins):
    """Owner: "he keeps saying I played the video but he isn't doing anything". Windows
    'accepts' the play but the video stays paused: Nova presses the player's own key, and
    if that doesn't work either it says so."""
    media = FakeMedia([yt()], works=False)
    with pytest.raises(ToolError, match="didn't start"):
        await V.video({"actions": ["play"]}, ToolContext(settings), _media=media)
    assert ("press", pc.KEYS["k"]) in wins.calls                   # tried YouTube's own key


async def test_players_own_key_rescues_play(settings, wins):
    media = FakeMedia([yt()], works=False)
    wins.on_k = lambda: setattr(media.items[0], "status", "playing")
    out = await V.video({"actions": ["play"]}, ToolContext(settings), _media=media)
    assert out.startswith("Playing")


def test_a_maximised_window_is_not_full_screen():
    """Owner's second screen has no taskbar: a maximised Firefox covers it all, which counted
    as full screen, so 'full screen the video' did nothing and said it was done."""
    maximised = 0x16CF0000          # WS_OVERLAPPEDWINDOW | WS_VISIBLE | WS_MAXIMIZE
    video_full_screen = 0x96000000  # WS_POPUP | WS_VISIBLE | WS_CLIPSIBLINGS
    assert not pc.fullscreen_style(maximised) and pc.fullscreen_style(video_full_screen)


def _uia_with(monkeypatch, buttons):
    from assistant.tools import uia
    monkeypatch.setattr(uia, "UIA", type("U", (), {"elements": lambda s, h: list(buttons)})())


def _grid_ctx(settings, on_click=None):
    from assistant.tools import grid as G
    from tests.test_grid import FakeMouse, FakeOverlay
    mouse = FakeMouse()
    real_click = mouse.click

    def click(button="left", double=False):
        real_click(button, double)
        if on_click:
            on_click()
    mouse.click = click
    return ToolContext(settings, services={"grid": G.GridController(mouse=mouse, overlay=FakeOverlay())}), mouse


async def test_skip_ad_clicks_youtubes_skip_button_and_checks(settings, wins, monkeypatch):
    from assistant.tools import grid as G, uia
    buttons = [uia.Element("Skip navigation", "button", G.Region(0, 0, 10, 10)),
               uia.Element("Skip Ad", "button", G.Region(1700, 800, 80, 40))]
    _uia_with(monkeypatch, buttons)
    ctx, mouse = _grid_ctx(settings, on_click=lambda: buttons.pop())          # the ad goes away
    assert match_intent("skip the ad") == ("video", {"actions": ["skip_ad"]})
    out = await V.video({"actions": ["skip_ad"]}, ctx, _media=FakeMedia([YT]))
    assert out.startswith("Skipped the ad") and ("move", 1740, 820) in mouse.log


async def test_skip_ad_that_isnt_there_or_doesnt_go_says_so(settings, wins, monkeypatch):
    from assistant.tools import grid as G, uia
    _uia_with(monkeypatch, [])
    ctx, _ = _grid_ctx(settings)
    with pytest.raises(ToolError, match="can't see a Skip button"):
        await V.video({"actions": ["skip_ad"]}, ctx, _media=FakeMedia([YT]))
    _uia_with(monkeypatch, [uia.Element("Skip", "button", G.Region(1700, 800, 80, 40))])   # click does nothing
    with pytest.raises(ToolError, match="still showing"):
        await V.video({"actions": ["skip_ad"]}, ctx, _media=FakeMedia([YT]))


@pytest.mark.parametrize("text,action,seconds", [
    ("skip ahead 30 seconds", "forward", 30), ("rewind 10 seconds", "back", 10),
    ("go back a minute in the video", "back", 60), ("fast forward two minutes", "forward", 120),
])
def test_seeking_by_time(text, action, seconds):
    assert match_intent(text) == ("video", {"actions": [action], "seconds": seconds})


async def test_seeking_presses_youtubes_ten_second_keys(settings, wins):
    out = await V.video({"actions": ["forward"], "seconds": 35}, ToolContext(settings), _media=FakeMedia([YT]))
    presses = [c[1] for c in wins.calls if c[0] == "press"]
    assert presses == [pc.KEYS["l"]] * 3 + [pc.KEYS["right"]] and out.startswith("Skipped ahead 35 seconds")
    wins.calls.clear()
    out = await V.video({"actions": ["back"], "seconds": 60}, ToolContext(settings), _media=FakeMedia([YT]))
    assert [c[1] for c in wins.calls if c[0] == "press"] == [pc.KEYS["j"]] * 6 and out.startswith("Went back 1 minute")
