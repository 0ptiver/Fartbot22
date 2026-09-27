"""Browser video control: finds the video via Windows' media list, exact play/pause, keys
go to the right browser window."""

import pytest

from assistant.brain.intents import match_intent
from assistant.tools import music, pc, video as V
from assistant.tools.registry import ToolContext, ToolError


class FakeMedia:
    def __init__(self, items):
        self.items, self.commands = items, []

    async def list(self):
        return self.items

    async def command(self, media, action):
        self.commands.append((media.title, action))
        return True


class FakeWindows:
    def __init__(self, wins):
        self.wins, self.calls = wins, []

    def list(self):
        return self.wins

    def show(self, hwnd, how):
        self.calls.append(("show", hwnd, how))

    def press(self, vk):
        self.calls.append(("press", vk))


YT = V.Media(0, "308046B0AF4A39CB", "Streamer Gets STALKED by Lil Vape!", "MoreCrown", "paused")
SPOTIFY = V.Media(1, "Spotify.exe", "My Way", "Kanye West", "playing")
WINDOWS = [pc.Win(1, "Nova", "msedge.exe"),                                  # the HUD: never this
           pc.Win(2, "Windows PowerShell", "WindowsTerminal.exe"),
           pc.Win(3, "(1) Streamer Gets STALKED by Lil Vape! - YouTube — Mozilla Firefox", "firefox.exe")]


@pytest.fixture
def wins(monkeypatch):
    fake = FakeWindows(list(WINDOWS))
    monkeypatch.setattr(pc, "WINDOWS", fake)
    monkeypatch.setattr(V.asyncio, "sleep", _no_sleep)
    return fake


async def _no_sleep(*a):
    return None


async def test_owners_case_fullscreen_then_play(settings, wins):
    """'Can you full screen the video and press play' got 'I don't know which video you mean'."""
    intent = match_intent("Can you full screen the video and press play")
    assert intent == ("video", {"actions": ["fullscreen", "play"]})
    media = FakeMedia([SPOTIFY, YT])
    out = await V.video(intent[1], ToolContext(settings), _media=media)
    assert out == "Fullscreen, playing: Streamer Gets STALKED by Lil Vape!."
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
    assert out.startswith("Fullscreen") and pressed == ["play_pause"]
