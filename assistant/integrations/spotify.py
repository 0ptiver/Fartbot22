"""Spotify Web API client with PKCE login (no client secret, no password ever seen by Nova).

One-time setup: create a free app at developer.spotify.com/dashboard with the redirect URI
http://127.0.0.1:8888/callback, then run `python -m assistant spotify login`. The refresh
token is stored in Windows Credential Manager and can be revoked any time from your Spotify
account (Apps) or with `python -m assistant spotify logout`.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import httpx

from assistant.core.secrets import get_secret, set_secret

AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API = "https://api.spotify.com/v1"
REDIRECT_URI = "http://127.0.0.1:8888/callback"   # Spotify requires a loopback IP, not "localhost"
SCOPES = ("user-read-playback-state user-modify-playback-state user-read-currently-playing "
          "user-read-recently-played user-library-read playlist-read-private")


class SpotifyError(Exception):
    """A problem worth telling the user about, in plain words."""


class NotLinked(SpotifyError):
    pass


# --- PKCE login -------------------------------------------------------------------
def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def authorize_url(client_id: str, challenge: str, state: str) -> str:
    return AUTH_URL + "?" + urlencode({
        "client_id": client_id, "response_type": "code", "redirect_uri": REDIRECT_URI,
        "scope": SCOPES, "code_challenge_method": "S256", "code_challenge": challenge,
        "state": state,
    })


async def exchange_code(http: httpx.AsyncClient, client_id: str, code: str, verifier: str) -> dict:
    r = await http.post(TOKEN_URL, data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT_URI,
        "client_id": client_id, "code_verifier": verifier})
    if r.status_code != 200:
        raise SpotifyError(f"Spotify login failed: {r.text[:200]}")
    return r.json()


async def wait_for_callback(state: str, timeout: float = 180) -> str:
    """One-shot HTTP server on 127.0.0.1:8888 that receives the login redirect."""
    from urllib.parse import parse_qs, urlparse

    loop = asyncio.get_running_loop()
    result: asyncio.Future[str] = loop.create_future()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        line = (await reader.readline()).decode("latin-1")
        parts = line.split(" ")
        query = parse_qs(urlparse(parts[1]).query) if len(parts) > 1 else {}
        ok = query.get("state", [""])[0] == state and "code" in query
        body = ("Nova is linked to Spotify. You can close this tab." if ok
                else "Spotify login failed or was cancelled. You can close this tab.")
        writer.write(f"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: {len(body)}"
                     f"\r\nConnection: close\r\n\r\n{body}".encode())
        await writer.drain()
        writer.close()
        if not result.done():
            if ok:
                result.set_result(query["code"][0])
            elif "error" in query or query.get("state", [""])[0] != state:
                result.set_exception(SpotifyError(query.get("error", ["state mismatch"])[0]))

    server = await asyncio.start_server(handle, "127.0.0.1", 8888)
    try:
        return await asyncio.wait_for(result, timeout)
    finally:
        server.close()


async def login(client_id: str, open_browser=None) -> dict:
    import webbrowser

    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(16)
    url = authorize_url(client_id, challenge, state)
    waiter = asyncio.create_task(wait_for_callback(state))
    await asyncio.sleep(0.1)
    (open_browser or webbrowser.open)(url)
    code = await waiter
    async with httpx.AsyncClient(timeout=15) as http:
        tokens = await exchange_code(http, client_id, code, verifier)
    set_secret("SPOTIFY_CLIENT_ID", client_id)
    set_secret("SPOTIFY_REFRESH_TOKEN", tokens["refresh_token"])
    return tokens


def logout() -> None:
    import keyring

    from assistant.core.secrets import KEYRING_SERVICE
    try:
        keyring.delete_password(KEYRING_SERVICE, "SPOTIFY_REFRESH_TOKEN")
    except Exception:
        pass


# --- API client ------------------------------------------------------------------------
class Spotify:
    def __init__(self, http: httpx.AsyncClient | None = None, open_app=None):
        self.http = http or httpx.AsyncClient(timeout=10)
        self._token: str | None = None
        self._expires = 0.0
        self._open_app = open_app      # starts the Spotify desktop app when no device is found

    @staticmethod
    def linked() -> bool:
        return bool(get_secret("SPOTIFY_CLIENT_ID") and get_secret("SPOTIFY_REFRESH_TOKEN"))

    async def _access_token(self) -> str:
        if self._token and time.time() < self._expires - 30:
            return self._token
        client_id, refresh = get_secret("SPOTIFY_CLIENT_ID"), get_secret("SPOTIFY_REFRESH_TOKEN")
        if not client_id or not refresh:
            raise NotLinked("Spotify isn't linked yet. Run: python -m assistant spotify login")
        r = await self.http.post(TOKEN_URL, data={
            "grant_type": "refresh_token", "refresh_token": refresh, "client_id": client_id})
        if r.status_code != 200:
            raise NotLinked("Spotify access was revoked or expired. Run: python -m assistant spotify login")
        data = r.json()
        self._token, self._expires = data["access_token"], time.time() + data.get("expires_in", 3600)
        if data.get("refresh_token"):         # Spotify may rotate the refresh token
            set_secret("SPOTIFY_REFRESH_TOKEN", data["refresh_token"])
        return self._token

    async def _call(self, method: str, path: str, **kw) -> Any:
        headers = {"Authorization": f"Bearer {await self._access_token()}"}
        r = await self.http.request(method, API + path, headers=headers, **kw)
        if r.status_code == 204 or not r.content:
            return None
        if r.status_code == 404 and "NO_ACTIVE_DEVICE" in r.text:
            raise SpotifyError("NO_ACTIVE_DEVICE")
        if r.status_code == 403:
            raise SpotifyError("Spotify refused that. Playback control needs Spotify Premium.")
        if r.status_code == 429:
            raise SpotifyError("Spotify is rate limiting requests. Try again in a moment.")
        if r.status_code >= 400:
            raise SpotifyError(f"Spotify error {r.status_code}: {r.text[:150]}")
        return r.json()

    # --- devices ---------------------------------------------------------------------
    async def device_id(self) -> str:
        """An active device, or this PC's Spotify app (starting it if needed)."""
        for attempt in range(12):
            devices = (await self._call("GET", "/me/player/devices") or {}).get("devices", [])
            active = [d for d in devices if d.get("is_active")]
            computers = [d for d in devices if d.get("type") == "Computer"]
            pick = (active or computers or devices or [None])[0]
            if pick:
                return pick["id"]
            if attempt == 0 and self._open_app:
                self._open_app()
            await asyncio.sleep(0.75)
        raise SpotifyError("I couldn't find a Spotify player. Is the Spotify app open and signed in?")

    async def _player(self, method: str, path: str, **kw) -> Any:
        try:
            return await self._call(method, path, **kw)
        except SpotifyError as e:
            if str(e) != "NO_ACTIVE_DEVICE":
                raise
            device = await self.device_id()
            params = dict(kw.pop("params", {}) or {}, device_id=device)
            return await self._call(method, path, params=params, **kw)

    # --- actions -----------------------------------------------------------------------
    async def search(self, query: str, kind: str) -> dict | None:
        data = await self._call("GET", "/search", params={"q": query, "type": kind, "limit": 5})
        items = [i for i in ((data or {}).get(kind + "s") or {}).get("items", []) if i]
        return items[0] if items else None

    async def play(self, query: str | None = None, kind: str = "auto") -> str:
        device = await self.device_id()
        body: dict[str, Any] = {}
        label = "your music"
        if kind == "liked_songs":
            saved = await self._call("GET", "/me/tracks", params={"limit": 50})
            uris = [i["track"]["uri"] for i in (saved or {}).get("items", []) if i.get("track")]
            if not uris:
                raise SpotifyError("You don't have any liked songs yet.")
            body, label = {"uris": uris}, "your liked songs"
        elif kind == "recently_played":
            recent = await self._call("GET", "/me/player/recently-played", params={"limit": 1})
            items = (recent or {}).get("items", [])
            if not items:
                raise SpotifyError("I can't see anything you've played recently.")
            t = items[0]["track"]
            body, label = {"uris": [t["uri"]]}, f"{t['name']} by {t['artists'][0]['name']}"
        elif query:
            kinds = [kind] if kind in ("track", "artist", "album", "playlist") else ["track", "artist", "playlist"]
            item = None
            for k in kinds:
                item = await self.search(query, k)
                if item:
                    break
            if not item:
                raise SpotifyError(f"I couldn't find \"{query}\" on Spotify.")
            if item["type"] == "track":
                body = {"uris": [item["uri"]]}
                label = f"{item['name']} by {item['artists'][0]['name']}"
            else:
                body = {"context_uri": item["uri"]}
                label = item["name"] + (" (playlist)" if item["type"] == "playlist" else "")
        else:
            label = "where you left off"
        await self._call("PUT", "/me/player/play", params={"device_id": device}, json=body)
        return f"Playing {label}."

    async def control(self, action: str, level: int | None = None) -> str:
        if action == "pause":
            await self._player("PUT", "/me/player/pause")
            return "Paused."
        if action == "resume":
            await self._player("PUT", "/me/player/play")
            return "Resumed."
        if action == "next":
            await self._player("POST", "/me/player/next")
            return "Skipped."
        if action == "previous":
            await self._player("POST", "/me/player/previous")
            return "Going back."
        if action in ("shuffle_on", "shuffle_off"):
            await self._player("PUT", "/me/player/shuffle", params={"state": str(action == "shuffle_on").lower()})
            return "Shuffle " + ("on." if action == "shuffle_on" else "off.")
        if action in ("repeat_on", "repeat_off"):
            await self._player("PUT", "/me/player/repeat", params={"state": "context" if action == "repeat_on" else "off"})
            return "Repeat " + ("on." if action == "repeat_on" else "off.")
        if action == "volume":
            if level is None:
                raise SpotifyError("Tell me a volume from 0 to 100.")
            await self._player("PUT", "/me/player/volume", params={"volume_percent": max(0, min(100, level))})
            return f"Spotify volume {max(0, min(100, level))}%."
        raise SpotifyError(f"Unknown music action: {action}")

    async def now_playing(self) -> str:
        data = await self._call("GET", "/me/player/currently-playing")
        if not data or not data.get("item"):
            return "Nothing is playing on Spotify."
        t = data["item"]
        artists = ", ".join(a["name"] for a in t.get("artists", []))
        state = "Playing" if data.get("is_playing") else "Paused on"
        return f"{state} {t['name']} by {artists}."

    async def queue(self, query: str) -> str:
        item = await self.search(query, "track")
        if not item:
            raise SpotifyError(f"I couldn't find \"{query}\" on Spotify.")
        await self._player("POST", "/me/player/queue", params={"uri": item["uri"]})
        return f"Queued {item['name']} by {item['artists'][0]['name']}."

    async def me(self) -> str:
        data = await self._call("GET", "/me")
        return f"{data.get('display_name') or data.get('id')} ({data.get('product', '?')})"
