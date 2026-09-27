"""Spotify client and music tools against a scripted fake Spotify API."""

import base64
import hashlib
import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from assistant.integrations import spotify as sp
from assistant.tools import music
from assistant.tools.registry import ToolError


@pytest.fixture
def secrets(monkeypatch):
    store = {"SPOTIFY_CLIENT_ID": "cid", "SPOTIFY_REFRESH_TOKEN": "rt1"}
    monkeypatch.setattr(sp, "get_secret", store.get)
    monkeypatch.setattr(sp, "set_secret", store.__setitem__)
    return store


NAMES = {"spotify:track:1": ("God's Plan", "Drake"), "spotify:track:7": ("Hotline Bling", "Drake"),
         "spotify:track:c": ("My Way", "Kanye West"), "spotify:track:a": ("My Way", "Fetty Wap")}


@pytest.fixture(autouse=True)
def fast_verify(monkeypatch):
    monkeypatch.setattr(sp.Spotify, "verify_delay", 0)


class FakeSpotify:
    def __init__(self, devices=None, premium=True, playing=None, starts=True, starts_after_transfer=False):
        self.devices = devices if devices is not None else [{"id": "pc1", "is_active": False, "type": "Computer", "name": "PC"}]
        self.premium, self.playing, self.calls = premium, playing, []
        self.starts, self.starts_after_transfer = starts, starts_after_transfer
        self.state = None
        self.transferred = False
        self.relink = {}

    def handler(self, req: httpx.Request):
        path, q = req.url.path, dict(req.url.params)
        body = json.loads(req.content) if req.content and req.headers.get("content-type", "").startswith("application/json") else None
        self.calls.append((req.method, path, q, body))
        if path == "/api/token":
            form = parse_qs(req.content.decode())
            assert form["client_id"] == ["cid"]
            return httpx.Response(200, json={"access_token": "at", "expires_in": 3600, "refresh_token": "rt2"})
        assert req.headers["authorization"] == "Bearer at"
        if path == "/v1/me/player/devices":
            return httpx.Response(200, json={"devices": self.devices})
        if path == "/v1/search":
            kind = q["type"]
            items = {"track": [{"type": "track", "uri": "spotify:track:1", "name": "God's Plan",
                                "artists": [{"name": "Drake"}]}],
                     "playlist": [None, {"type": "playlist", "uri": "spotify:playlist:9", "name": "Chill"}]}
            return httpx.Response(200, json={kind + "s": {"items": items.get(kind, [])}})
        if path == "/v1/me/player/play":
            if not self.premium:
                return httpx.Response(403, json={"error": {"reason": "PREMIUM_REQUIRED"}})
            if self.starts or (self.starts_after_transfer and self.transferred):
                uri = (body or {}).get("uris", [None])[0] or "spotify:track:ctx"
                name, artist = NAMES.get(uri, ("Some Song", "Someone"))
                uri = self.relink.get(uri, uri)
                self.state = {"is_playing": True, "item": {"uri": uri, "name": name, "artists": [{"name": artist}]},
                              "context": {"uri": (body or {}).get("context_uri")}, "device": {"name": "PC"}}
            return httpx.Response(204)
        if path == "/v1/me/player" and req.method == "PUT":
            self.transferred = True
            return httpx.Response(204)
        if path == "/v1/me/player" and req.method == "GET":
            return httpx.Response(200, json=self.state) if self.state else httpx.Response(204)
        if path == "/v1/me/player/recently-played":
            return httpx.Response(200, json={"items": [{"track": {"uri": "spotify:track:7", "name": "Hotline Bling", "artists": [{"name": "Drake"}]}}]})
        if path == "/v1/me/player/currently-playing":
            return httpx.Response(200, json=self.playing) if self.playing else httpx.Response(204)
        if path in ("/v1/me/player/pause", "/v1/me/player/next", "/v1/me/player/volume", "/v1/me/player/queue"):
            if not q.get("device_id") and not any(d.get("is_active") for d in self.devices):
                return httpx.Response(404, json={"error": {"status": 404, "reason": "NO_ACTIVE_DEVICE"}})
            return httpx.Response(204)
        return httpx.Response(404, text="unexpected " + path)

    def client(self, **kw):
        return sp.Spotify(http=httpx.AsyncClient(transport=httpx.MockTransport(self.handler)), **kw)


def test_pkce_and_authorize_url():
    verifier, challenge = sp.pkce_pair()
    assert 43 <= len(verifier) <= 128
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert challenge == expected
    q = parse_qs(urlparse(sp.authorize_url("cid", challenge, "st")).query)
    assert q["redirect_uri"] == ["http://127.0.0.1:8888/callback"]
    assert q["code_challenge_method"] == ["S256"] and q["state"] == ["st"]
    assert "user-modify-playback-state" in q["scope"][0]


async def test_play_track_and_token_rotation(secrets):
    fake = FakeSpotify()
    out = await fake.client().play("gods plan", "auto")
    assert out == "Playing God's Plan by Drake on PC."
    play = [c for c in fake.calls if c[1] == "/v1/me/player/play"][0]
    assert play[2]["device_id"] == "pc1" and play[3] == {"uris": ["spotify:track:1"]}
    assert secrets["SPOTIFY_REFRESH_TOKEN"] == "rt2"          # rotated token saved


async def test_playlist_skips_null_items(secrets):
    fake = FakeSpotify()
    out = await fake.client().play("chill", "playlist")
    assert out == "Playing Chill (playlist) on PC."


async def test_recently_played(secrets):
    fake = FakeSpotify()
    assert await fake.client().play(None, "recently_played") == "Playing Hotline Bling by Drake on PC."


async def test_opens_app_when_no_device(secrets, monkeypatch):
    fake = FakeSpotify(devices=[])
    opened = []

    def open_app():
        opened.append(True)
        fake.devices.append({"id": "pc1", "is_active": False, "type": "Computer", "name": "PC"})

    monkeypatch.setattr(sp.asyncio, "sleep", lambda s: _noop())
    assert (await fake.client(open_app=open_app).play("gods plan")).startswith("Playing")
    assert opened == [True]


async def _noop():
    return None


async def test_no_active_device_retries_with_device(secrets):
    fake = FakeSpotify()
    assert await fake.client().control("pause") == "Paused."
    pauses = [c for c in fake.calls if c[1] == "/v1/me/player/pause"]
    assert len(pauses) == 2 and pauses[1][2]["device_id"] == "pc1"


async def test_premium_and_now_playing(secrets):
    fake = FakeSpotify(premium=False)
    with pytest.raises(sp.SpotifyError, match="Premium"):
        await fake.client().play("gods plan")
    assert await FakeSpotify().client().now_playing() == "Nothing is playing on Spotify."
    playing = {"is_playing": True, "item": {"name": "One Dance", "artists": [{"name": "Drake"}, {"name": "Wizkid"}]}}
    assert await FakeSpotify(playing=playing).client().now_playing() == "Playing One Dance by Drake, Wizkid."


async def test_not_linked(monkeypatch):
    monkeypatch.setattr(sp, "get_secret", lambda k: None)
    with pytest.raises(sp.NotLinked, match="spotify login"):
        await FakeSpotify().client().play("x")


async def test_music_control_falls_back_to_media_keys(monkeypatch, ctx):
    monkeypatch.setattr(sp.Spotify, "linked", staticmethod(lambda: False))
    pressed = []
    monkeypatch.setattr(music, "press_media_key", pressed.append)
    assert await music.music_control({"action": "next"}, ctx) == "Skipped."
    assert pressed == ["next"]
    with pytest.raises(ToolError, match="spotify login"):
        await music.music_control({"action": "shuffle_on"}, ctx)


async def test_tool_errors_are_friendly(monkeypatch, ctx, secrets):
    fake = FakeSpotify(premium=False)
    ctx.services["spotify"] = fake.client()
    with pytest.raises(ToolError, match="Premium"):
        await music.play_music({"query": "gods plan"}, ctx)


async def test_login_callback_flow(secrets, monkeypatch):
    """Browser redirect -> local callback server -> token exchange -> stored."""
    captured = {}

    def fake_browser(url):
        import asyncio
        q = parse_qs(urlparse(url).query)
        captured["challenge"] = q["code_challenge"][0]

        async def hit():
            r, w = await asyncio.open_connection("127.0.0.1", 8888)
            w.write(f"GET /callback?code=abc&state={q['state'][0]} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
            await w.drain()
            await r.read()
            w.close()
        asyncio.get_running_loop().create_task(hit())

    async def fake_exchange(http, client_id, code, verifier):
        assert code == "abc"
        assert base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode() == captured["challenge"]
        return {"access_token": "at", "refresh_token": "rt-new"}

    monkeypatch.setattr(sp, "exchange_code", fake_exchange)
    await sp.login("cid", open_browser=fake_browser)
    assert secrets["SPOTIFY_REFRESH_TOKEN"] == "rt-new"


async def test_login_rejects_wrong_state(secrets):
    import asyncio

    async def attacker():
        await asyncio.sleep(0.1)
        r, w = await asyncio.open_connection("127.0.0.1", 8888)
        w.write(b"GET /callback?code=evil&state=WRONG HTTP/1.1\r\n\r\n")
        await w.drain()
        await r.read()
        w.close()

    asyncio.get_running_loop().create_task(attacker())
    with pytest.raises(sp.SpotifyError):
        await sp.wait_for_callback("real-state", timeout=3)


def test_split_by():
    assert sp.split_by("My Way by Kanye West") == ("My Way", "Kanye West")
    assert sp.split_by("Stand by Me") == ("Stand", "Me")          # ambiguous; the ranking copes
    assert sp.split_by("gods plan") == ("gods plan", "")


class SearchFake(FakeSpotify):
    """Search results depend on the query, like the real API."""

    def handler(self, req):
        if req.url.path == "/v1/search":
            q, kind = req.url.params["q"], req.url.params["type"]
            self.calls.append(("GET", "/v1/search", {"q": q, "type": kind}, None))
            if kind == "artist":
                items = [{"type": "artist", "uri": "spotify:artist:drake", "name": "Drake"}] if "drake" in q.lower() else []
            elif kind == "track" and 'artist:"kanye west"' in q:
                items = []                                   # field search finds nothing -> fallback
            elif kind == "track":
                items = [
                    {"type": "track", "uri": "spotify:track:a", "name": "My Way", "artists": [{"name": "Fetty Wap"}]},
                    {"type": "track", "uri": "spotify:track:b", "name": "My Way (Remix)", "artists": [{"name": "Someone"}]},
                    {"type": "track", "uri": "spotify:track:c", "name": "My Way", "artists": [{"name": "Kanye West"}]},
                ]
            else:
                items = []
            return httpx.Response(200, json={kind + "s": {"items": items}})
        return super().handler(req)


async def test_ranking_prefers_matching_artist(secrets):
    fake = SearchFake()
    out = await fake.client().play("my way by kanye west")
    assert out == "Playing My Way by Kanye West on PC."
    queries = [c[2]["q"] for c in fake.calls if c[1] == "/v1/search"]
    assert 'track:"my way" artist:"kanye west"' in queries      # tried the precise search first


async def test_bare_artist_plays_artist(secrets):
    fake = SearchFake()
    assert await fake.client().play("Drake") == "Playing Drake on PC."
    play = [c for c in fake.calls if c[1] == "/v1/me/player/play"][0]
    assert play[3] == {"context_uri": "spotify:artist:drake"}



def test_pick_device_prefers_this_pc():
    devs = [{"id": "web", "type": "Computer", "name": "Web Player (Chrome)", "is_active": True},
            {"id": "phone", "type": "Smartphone", "name": "iPhone"},
            {"id": "me", "type": "Computer", "name": "PATTO-LAPTOP"}]
    assert sp.Spotify.pick_device(devs, "patto-laptop")["id"] == "me"
    assert sp.Spotify.pick_device(devs, "other")["id"] == "web"          # nothing named: active one
    devs2 = [{"id": "web", "type": "Computer", "name": "Web Player (Chrome)"},
             {"id": "app", "type": "Computer", "name": "GAMING-PC"}]
    assert sp.Spotify.pick_device(devs2, "x")["id"] == "app"             # desktop app over web tab
    assert sp.Spotify.pick_device([{"id": "r", "type": "Computer", "name": "PC", "is_restricted": True}], "x") is None


async def test_not_actually_playing_is_reported(secrets):
    fake = FakeSpotify(starts=False)
    with pytest.raises(sp.SpotifyError, match="nothing is playing on PC"):
        await fake.client().play("gods plan")


async def test_transfer_then_retry_recovers(secrets):
    fake = FakeSpotify(starts=False, starts_after_transfer=True)
    assert (await fake.client().play("gods plan")) == "Playing God's Plan by Drake on PC."
    assert fake.transferred



async def test_relinked_track_counts_as_playing(secrets):
    """Spotify may play a market-specific copy of the song with a different URI."""
    fake = FakeSpotify()
    fake.relink = {"spotify:track:1": "spotify:track:1-US"}
    assert await fake.client().play("gods plan") == "Playing God's Plan by Drake on PC."


async def test_this_pc_name_is_not_spoken(secrets, monkeypatch):
    import socket
    monkeypatch.setattr(socket, "gethostname", lambda: "PC")
    assert await FakeSpotify().client().play("gods plan") == "Playing God's Plan by Drake."


# --- what the PC really plays (owner: "he keeps saying I'm playing this on Spotify but nothing is happening")
class WindowsMedia:
    """Windows' list of what's playing (the volume pop-up's list)."""

    def __init__(self, items=(), available=True):
        self.items, self._available = list(items), available

    def available(self):
        return self._available

    async def list(self):
        return list(self.items)


@pytest.fixture
def pc_media(monkeypatch):
    from assistant.tools import video
    fake = WindowsMedia()
    monkeypatch.setattr(video, "MEDIA", fake)
    monkeypatch.setattr(music, "HEAR_S", 0)
    return fake


def spotify_ctx(ctx, fake):
    ctx.services["spotify"] = fake.client()
    return ctx


async def test_said_playing_only_when_the_pc_plays_it(secrets, ctx, pc_media):
    from assistant.tools.video import Media
    pc_media.items = [Media(0, "Spotify.exe", "God's Plan", "Drake", "playing")]
    out = await music.play_music({"query": "gods plan"}, spotify_ctx(ctx, FakeSpotify()))
    assert out.startswith("Playing God's Plan by Drake")


async def test_spotify_says_ok_but_the_pc_is_silent(secrets, ctx, pc_media, monkeypatch):
    from assistant.tools.video import Media
    pc_media.items = [Media(0, "Spotify.exe", "Old Song", "Someone", "paused")]
    tried = []

    async def app_button(target, ctx):
        tried.append(target["uri"])
        return False                                   # couldn't press Play in the app either
    monkeypatch.setattr(music, "play_in_app", app_button)
    with pytest.raises(ToolError, match="didn't start playing God's Plan"):
        await music.play_music({"query": "gods plan"}, spotify_ctx(ctx, FakeSpotify()))
    assert tried == ["spotify:track:1"]


async def test_the_apps_own_play_button_rescues_it(secrets, ctx, pc_media, monkeypatch):
    from assistant.tools.video import Media
    pc_media.items = [Media(0, "Spotify.exe", "Old Song", "Someone", "paused")]

    async def app_button(target, ctx):
        pc_media.items = [Media(0, "Spotify.exe", "God's Plan", "Drake", "playing")]
        return True
    monkeypatch.setattr(music, "play_in_app", app_button)
    out = await music.play_music({"query": "gods plan"}, spotify_ctx(ctx, FakeSpotify(starts=False)))
    assert out == "Playing God's Plan by Drake."


async def test_stale_old_song_is_not_success(secrets):
    """Spotify's servers still say the old song is playing: that's not the new one."""
    fake = FakeSpotify(starts=False)
    fake.state = {"is_playing": True, "item": {"uri": "spotify:track:old", "name": "Old Song",
                                               "artists": [{"name": "Someone"}]}, "context": None}
    with pytest.raises(sp.NotPlaying):
        await fake.client().play("gods plan")


async def test_without_windows_media_spotifys_word_is_used(secrets, ctx, pc_media):
    pc_media._available = False
    out = await music.play_music({"query": "gods plan"}, spotify_ctx(ctx, FakeSpotify()))
    assert out.startswith("Playing God's Plan")


def test_play_button_is_the_pages_not_the_player_bars(monkeypatch):
    from types import SimpleNamespace as NS

    from assistant.tools import pc
    monkeypatch.setattr(pc, "WINDOWS", NS(rect=lambda h: (0, 0, 1000, 800)))
    R = lambda y: NS(x=10, y=y, w=40, h=40)
    bar = NS(kind="button", name="Play", rect=R(740))                 # bottom player bar: old song
    big = NS(kind="button", name="Play", rect=R(300))                 # the page's green button
    row = NS(kind="button", name="Play God's Plan by Drake", rect=R(450))
    win = NS(hwnd=1)
    assert music.pick_play_button([bar, big], None, win) is big
    assert music.pick_play_button([bar, big, row], "God's Plan", win) is row
    assert music.pick_play_button([bar], None, win) is None


async def test_search_skips_songs_that_cant_play_here(secrets):
    class Unplayable(FakeSpotify):
        def handler(self, req):
            if req.url.path == "/v1/search" and req.url.params["type"] == "track":
                return httpx.Response(200, json={"tracks": {"items": [
                    {"type": "track", "uri": "spotify:track:x", "name": "God's Plan", "is_playable": False,
                     "artists": [{"name": "Drake"}]},
                    {"type": "track", "uri": "spotify:track:1", "name": "God's Plan", "artists": [{"name": "Drake"}]}]}})
            return super().handler(req)
    fake = Unplayable()
    await fake.client().play("gods plan", "track")
    play = [c for c in fake.calls if c[1] == "/v1/me/player/play"][0]
    assert play[3] == {"uris": ["spotify:track:1"]}


async def test_search_asks_for_this_country(secrets):
    fake = FakeSpotify()
    await fake.client().play("gods plan")
    search = [c for c in fake.calls if c[1] == "/v1/search"][0]
    assert search[2]["market"] == "from_token"
