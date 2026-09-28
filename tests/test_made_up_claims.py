"""Owner: "he gets more and more stupid the more you do, can you just actually fix the problems".
The screenshots: "Skipped." (nothing skipped), "Firefox is reopened" / "Now on YouTube" /
"Closed Spotify" (no tool ran at all), and "close Spotify" closing Firefox."""

import httpx
import pytest

from assistant.brain.intents import match_intent
from assistant.brain.llm import TextDelta, ToolStarted
from assistant.core.conversation import Conversation
from assistant.integrations import spotify as sp
from assistant.tools import pc, video as V
from assistant.tools.registry import ToolContext, ToolError
from tests.test_agent import FakeAgent, with_agent
from tests.test_local_brain import collect, make, text_reply, tool_reply


def said(events):
    return "".join(e.text for e in events if isinstance(e, TextDelta))


def tools(events):
    return [e.name for e in events if isinstance(e, ToolStarted)]


class NoMedia:
    """Windows' media list not answering (the 4.0 s on the owner's 'Media' chip)."""

    async def list(self):
        return []


class FakeSpotifyPlayer:
    def __init__(self, obeys=True):
        self.track, self.playing, self.obeys = 0, True, obeys
        self.names = ["God's Plan", "Hotline Bling"]

    def handler(self, req):
        path = req.url.path
        if path == "/api/token":
            return httpx.Response(200, json={"access_token": "at", "expires_in": 3600})
        if path == "/v1/me/player" and req.method == "GET":
            return httpx.Response(200, json={"is_playing": self.playing, "item": {
                "id": str(self.track), "name": self.names[self.track % 2], "artists": [{"name": "Drake"}]}})
        if path == "/v1/me/player/next" and self.obeys:
            self.track += 1
        if path == "/v1/me/player/pause" and self.obeys:
            self.playing = False
        return httpx.Response(204)


@pytest.fixture
def spotify_linked(monkeypatch):
    store = {"SPOTIFY_CLIENT_ID": "cid", "SPOTIFY_REFRESH_TOKEN": "rt"}
    monkeypatch.setattr(sp, "get_secret", store.get)
    monkeypatch.setattr(sp, "set_secret", store.__setitem__)
    monkeypatch.setattr(sp.Spotify, "verify_delay", 0)


async def test_owners_case_skip_is_checked_with_spotify(settings, spotify_linked):
    player = FakeSpotifyPlayer()
    ctx = ToolContext(settings, services={"spotify": sp.Spotify(http=httpx.AsyncClient(transport=httpx.MockTransport(player.handler)))})
    out = await V.media({"action": "next"}, ctx, _media=NoMedia())
    assert out == "Now playing Hotline Bling by Drake."
    out = await V.media({"action": "pause"}, ctx, _media=NoMedia())
    assert out == "Paused Hotline Bling." and not player.playing


async def test_a_skip_that_doesnt_happen_is_not_claimed(settings, spotify_linked):
    player = FakeSpotifyPlayer(obeys=False)
    ctx = ToolContext(settings, services={"spotify": sp.Spotify(http=httpx.AsyncClient(transport=httpx.MockTransport(player.handler)))})
    with pytest.raises(ToolError, match="nothing changed"):
        await V.media({"action": "next"}, ctx, _media=NoMedia())


async def test_without_spotify_the_key_press_is_called_unchecked(settings, monkeypatch):
    from assistant.tools import music
    monkeypatch.setattr(sp.Spotify, "linked", staticmethod(lambda: False))
    monkeypatch.setattr(music, "press_media_key", lambda k: None)
    out = await V.media({"action": "next"}, ToolContext(settings), _media=NoMedia())
    assert out != "Skipped." and "can't check" in out


@pytest.mark.parametrize("said_,calls", [
    ("You didn't skip anything", [("media", {"action": "next"})]),
    ("You still didn't skip the song.", [("media", {"action": "next"})]),
    ("You didn't pause it. Just go ahead and close Spotify.",
     [("media", {"action": "pause"}), ("window", {"action": "close", "app": "spotify"})]),
    ("You didn't take me to YouTube first of all and you still haven't closed Spotify",
     [("my_browser", {"action": "switch_tab", "tab": "youtube", "or_open": True}), ("window", {"action": "close", "app": "spotify"})]),
])
async def test_owners_complaints_are_done_directly(local_settings, ctx, said_, calls):
    brain, fake = make(local_settings, [])
    ran = []
    for name, _ in calls:
        brain.registry._tools[name].handler = lambda a, c, n=name: ran.append((n, a)) or "Done."
    await collect(brain, Conversation(), said_, ctx)
    assert ran == calls and not fake.requests                        # no model to make things up


@pytest.mark.parametrize("reply", ["Firefox is reopened, sir.", "Now on YouTube, sir.", "Closed Spotify. Now on YouTube, sir.",
                                   "Skipped the current song."])
async def test_made_up_claims_are_never_said(local_settings, ctx, reply):
    """The model 'did' things without any tool running."""
    brain, fake = make(local_settings, [text_reply(reply), text_reply(reply)])
    events = await collect(brain, Conversation(), "You just closed my Firefox, dude", ctx)
    assert said(events) == "Sorry, sir, I haven't actually done that. Could you say it another way?"


async def test_made_up_claim_goes_to_claude_when_on(local_settings, ctx):
    brain, fake = make(local_settings, [text_reply("Firefox is reopened, sir.")])
    agent = with_agent(brain, FakeAgent(say="Reopened Firefox.", steps=[
        {"tool": "open_app", "args": {"name": "firefox"}, "ok": True, "ms": 400, "result": "Opened Firefox."}]))
    events = await collect(brain, Conversation(), "You just closed my Firefox, dude", ctx)
    assert "Firefox is reopened" not in said(events) and said(events).startswith("Let me work that out")
    assert agent.calls


async def test_real_actions_and_answers_are_untouched(local_settings, ctx):
    brain, fake = make(local_settings, [tool_reply("volume", {"action": "up"}), text_reply("Turned it up, sir."),
                                        text_reply("The shop is closed on Sundays, sir.")])
    brain.registry._tools["volume"].handler = lambda a, c: "Volume 50."
    events = await collect(brain, Conversation(), "bump the sound a little would you", ctx)
    assert said(events) == "Turned it up, sir."                      # a tool really ran
    events = await collect(brain, Conversation(), "is the shop open on sunday", ctx)
    assert said(events) == "The shop is closed on Sundays, sir."     # an answer, not a claim


async def test_owners_case_close_spotify_never_closes_whats_in_front(local_settings, ctx):
    """The model called window close with app 'this' and Firefox went."""
    brain, fake = make(local_settings, [tool_reply("window", {"action": "close", "app": "this"}),
                                        tool_reply("window", {"action": "close", "app": "spotify"}),
                                        text_reply("Spotify is closed, sir.")])
    closed = []
    brain.registry._tools["window"].handler = lambda a, c: closed.append(a["app"]) or f"Closed {a['app']}."
    await collect(brain, Conversation(), "would you be a dear and get rid of spotify", ctx)
    assert closed == ["spotify"]


async def test_leaked_tool_text_is_not_shown(local_settings, ctx):
    brain, fake = make(local_settings, [tool_reply("volume", {"action": "up"}), text_reply(">volume up\n\nLouder, sir.")])
    brain.registry._tools["volume"].handler = lambda a, c: "Volume 50."
    events = await collect(brain, Conversation(), "bump the sound a little would you", ctx)
    assert said(events) == "Louder, sir."


def test_spotify_in_the_tray_is_quit(settings, monkeypatch):
    """'Close Spotify' with Spotify only by the clock (no window)."""
    monkeypatch.setattr(pc, "WINDOWS", type("W", (), {"list": lambda s: [pc.Win(3, "YouTube — Mozilla Firefox", "firefox.exe")],
                                                       "active": lambda s: pc.Win(3, "YouTube — Mozilla Firefox", "firefox.exe")})())
    from assistant.tools import mybrowser
    monkeypatch.setattr(mybrowser, "has_tab", lambda name: False)
    ended = []
    monkeypatch.setattr(pc, "end_processes", lambda exe: ended.append(exe) or 1)
    assert pc.window_control({"action": "close", "app": "spotify"}, ToolContext(settings)) == \
        "Quit Spotify (it was running in the tray)."
    assert ended == ["spotify.exe"]


@pytest.mark.parametrize("text,expected", [
    ("Take me back to YouTube.", ("my_browser", {"action": "switch_tab", "tab": "youtube", "or_open": True})),
    ("go back to spotify", ("window", {"action": "focus", "app": "spotify"})),
    ("go to sleep", None),
])
def test_where_to_go(text, expected):
    assert match_intent(text) == expected


async def test_a_stuck_media_list_is_not_asked_again_for_a_while(monkeypatch):
    """Every media command on the owner's PC waited 4 s for Windows' list (the 4.0 s chips)."""
    import asyncio
    calls = []

    async def hang(coro, timeout):
        calls.append(1)
        coro.close()
        raise asyncio.TimeoutError
    b = V.MediaBackend()
    monkeypatch.setattr(V.sys, "platform", "win32")
    monkeypatch.setattr(V, "apartment", lambda: "test")
    monkeypatch.setattr(V.WINRT, "run", hang)
    assert await b.list() == [] and b.stuck()
    assert await b.list() == [] and len(calls) == 1            # not asked (or waited on) again
    b._stuck_until = 0
    await b.list()
    assert len(calls) == 2                                      # asked again later


async def test_playing_a_song_trusts_spotify_when_windows_list_is_stuck(monkeypatch):
    from assistant.tools import music
    stuck = type("M", (), {"available": lambda s: True, "stuck": lambda s: True,
                           "list": lambda s: asyncio_list()})()
    monkeypatch.setattr(V, "MEDIA", stuck)
    assert await music.hear_spotify({"title": "God's Plan"}) is None           # straight away, no 30 s wait


async def asyncio_list():
    return []


async def test_news_about_things_being_on_or_open_is_not_a_claim(local_settings, ctx):
    brain, fake = make(local_settings, [text_reply("The Steam summer sale is now on, sir. It's open until July.")])
    events = await collect(brain, Conversation(), "tell me about the steam sale", ctx)
    assert said(events).startswith("The Steam summer sale is now on")
