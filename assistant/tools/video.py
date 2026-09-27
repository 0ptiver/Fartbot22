"""Videos in the browser: "full screen the video and press play".

Nova asks Windows which media is playing (the same list the volume pop-up shows: title,
app, playing or paused), so "play" and "pause" are exact (never a toggle that does the
opposite) and don't need the window in front. For keys like fullscreen it finds the browser
window showing that video (by its title), brings it to the front and presses the key.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import dataclass

from assistant.tools.registry import Risk, ToolContext, ToolError, ToolRegistry

log = logging.getLogger(__name__)

BROWSERS = ("firefox", "chrome", "msedge", "brave", "opera", "vivaldi", "arc", "librewolf", "waterfox")
MUSIC_APPS = ("spotify",)
# YouTube-style keys; also work on most players (Netflix, Twitch, Prime use f/space/m).
KEY_ACTIONS = {"fullscreen": "f", "exit_fullscreen": "escape", "mute": "m", "forward": "l", "back": "j"}
ACTIONS = ["play", "pause", "toggle", "fullscreen", "exit_fullscreen", "mute", "forward", "back",
           "next", "previous"]


@dataclass
class Media:
    id: int                 # index into the backend's current session list
    app: str                # e.g. "firefox.exe", "308046B0AF4A39CB" (Firefox), "Spotify.exe"
    title: str
    artist: str
    status: str             # playing | paused | stopped | other

    @property
    def is_music_app(self) -> bool:
        return any(m in self.app.lower() for m in MUSIC_APPS)


class MediaBackend:
    """Windows' media sessions (GlobalSystemMediaTransportControls). A fake in tests."""

    def __init__(self):
        self._sessions: list = []

    async def list(self) -> list[Media]:
        if sys.platform != "win32":
            return []
        try:
            from winrt.windows.media.control import (
                GlobalSystemMediaTransportControlsSessionManager as Manager)
        except ImportError:
            log.warning("winrt not installed: run scripts\\update.ps1")
            return []
        mgr = await Manager.request_async()
        self._sessions = list(mgr.get_sessions())
        out = []
        for i, s in enumerate(self._sessions):
            try:
                props = await s.try_get_media_properties_async()
                status = int(s.get_playback_info().playback_status)
            except Exception:
                continue
            out.append(Media(i, s.source_app_user_model_id or "", (props.title if props else "") or "",
                             (props.artist if props else "") or "",
                             {4: "playing", 5: "paused", 3: "stopped"}.get(status, "other")))
        return out

    async def command(self, media: Media, action: str) -> bool:
        s = self._sessions[media.id]
        fn = {"play": s.try_play_async, "pause": s.try_pause_async,
              "toggle": s.try_toggle_play_pause_async, "next": s.try_skip_next_async,
              "previous": s.try_skip_previous_async}[action]
        return bool(await fn())


MEDIA = MediaBackend()


def pick_video(items: list[Media]) -> Media | None:
    """The browser video the user means: not Spotify; playing beats paused beats the rest."""
    videos = [m for m in items if not m.is_music_app]
    if not videos:
        return None
    rank = {"playing": 0, "paused": 1}
    browser_first = sorted(videos, key=lambda m: (rank.get(m.status, 2),
                                                  0 if any(b in m.app.lower() for b in BROWSERS) else 1))
    return browser_first[0]


def find_video_window(media: Media | None):
    """The window showing the video: its title contains the video's title (browsers show the
    tab title), else a browser window on a video site, else any browser window (not Nova's)."""
    from assistant.tools import pc

    wins = [w for w in pc.WINDOWS.list() if w.title.strip().lower() not in ("nova", "")]
    browsers = [w for w in wins if w.process.lower().removesuffix(".exe") in BROWSERS]
    if media and media.title:
        want = media.title.lower()[:40]
        for w in wins:
            if want in w.title.lower():
                return w
    for site in ("youtube", "netflix", "twitch", "prime video", "disney", "hulu", "max", "crunchyroll"):
        for w in browsers:
            if site in w.title.lower():
                return w
    return browsers[0] if browsers else None


async def video(args: dict, ctx: ToolContext, _media: MediaBackend | None = None) -> str:
    from assistant.tools import pc

    media_api = _media or MEDIA
    actions = [a for a in args["actions"] if a in ACTIONS][:5]
    if not actions:
        raise ToolError("What should I do with the video?")
    items = await media_api.list()
    media = pick_video(items)
    done: list[str] = []
    window = None
    for action in actions:
        if action in KEY_ACTIONS:
            if window is None:
                window = await asyncio.to_thread(find_video_window, media)
                if window is None:
                    raise ToolError("I can't find a browser window with a video in it.")
                await asyncio.to_thread(pc.WINDOWS.show, window.hwnd, "focus")
                await asyncio.sleep(0.35)          # let it come to the front before the key
            await asyncio.to_thread(pc.WINDOWS.press, pc.KEYS[KEY_ACTIONS[action]])
            await asyncio.sleep(0.15)
            done.append({"fullscreen": "fullscreen", "exit_fullscreen": "out of fullscreen",
                         "mute": "mute toggled", "forward": "skipped ahead 10 seconds",
                         "back": "went back 10 seconds"}[action])
            continue
        if media is None:
            if action in ("play", "pause", "toggle"):
                # Nothing registered with Windows: fall back to the play/pause media key.
                from assistant.tools import music
                await asyncio.to_thread(music.press_media_key, "play_pause")
                done.append("pressed play/pause")
                continue
            raise ToolError("I can't find a video playing on this PC.")
        if action == "play" and media.status == "playing":
            done.append("already playing")
            continue
        if action == "pause" and media.status == "paused":
            done.append("already paused")
            continue
        ok = await media_api.command(media, action)
        if not ok:
            raise ToolError(f"The video didn't accept '{action}'.")
        if action in ("play", "pause"):
            media.status = "playing" if action == "play" else "paused"
        done.append({"play": "playing", "pause": "paused", "toggle": "play/pause pressed",
                     "next": "next video", "previous": "previous video"}[action])
    what = f": {media.title}" if media and media.title else ""
    text = ", ".join(done)
    return text[0].upper() + text[1:] + what + "."


def register(reg: ToolRegistry) -> None:
    reg.tool("video", "Control the video playing in the browser (YouTube, Netflix, Twitch...) or a "
             "video app. Finds it by itself: no need to ask which video. actions run in order, "
             "e.g. ['fullscreen', 'play']. play/pause are exact (not toggles).",
             {"type": "object", "properties": {
                 "actions": {"type": "array", "items": {"type": "string", "enum": ACTIONS},
                             "minItems": 1, "maxItems": 5}},
              "required": ["actions"], "additionalProperties": False},
             risk=Risk.SAFE, category="media")(video)
