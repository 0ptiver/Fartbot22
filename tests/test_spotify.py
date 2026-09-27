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


class FakeSpotify:
    def __init__(self, devices=None, premium=True, playing=None):
        self.devices = devices if devices is not None else [{"id": "pc1", "is_active": False, "type": "Computer", "name": "PC"}]
        self.premium, self.playing, self.calls = premium, playing, []

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
            return httpx.Response(204)
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
    assert out == "Playing God's Plan by Drake."
    play = [c for c in fake.calls if c[1] == "/v1/me/player/play"][0]
    assert play[2]["device_id"] == "pc1" and play[3] == {"uris": ["spotify:track:1"]}
    assert secrets["SPOTIFY_REFRESH_TOKEN"] == "rt2"          # rotated token saved


async def test_playlist_skips_null_items(secrets):
    fake = FakeSpotify()
    out = await fake.client().play("chill", "playlist")
    assert out == "Playing Chill (playlist)."


async def test_recently_played(secrets):
    fake = FakeSpotify()
    assert await fake.client().play(None, "recently_played") == "Playing Hotline Bling by Drake."


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
    assert out == "Playing My Way by Kanye West."
    queries = [c[2]["q"] for c in fake.calls if c[1] == "/v1/search"]
    assert 'track:"my way" artist:"kanye west"' in queries      # tried the precise search first


async def test_bare_artist_plays_artist(secrets):
    fake = SearchFake()
    assert await fake.client().play("Drake") == "Playing Drake."
    play = [c for c in fake.calls if c[1] == "/v1/me/player/play"][0]
    assert play[3] == {"context_uri": "spotify:artist:drake"}
