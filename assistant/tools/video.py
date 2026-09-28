"""Videos in the browser: "full screen the video and press play".

Nova asks Windows which media is playing (the same list the volume pop-up shows: title,
app, playing or paused), so "play" and "pause" are exact (never a toggle that does the
opposite) and don't need the window in front. For keys like fullscreen it finds the browser
window showing that video (by its title), brings it to the front and presses the key.
"""

from __future__ import annotations

import asyncio
import threading
import time
import logging
import sys
from dataclasses import dataclass

from assistant.tools.registry import Risk, ToolContext, ToolError, ToolRegistry

log = logging.getLogger(__name__)

BROWSERS = ("firefox", "chrome", "msedge", "brave", "opera", "vivaldi", "arc", "librewolf", "waterfox")
MUSIC_APPS = ("spotify",)
# YouTube-style keys; also work on most players (Netflix, Twitch, Prime use f/space/m).
KEY_ACTIONS = {"fullscreen": "f", "exit_fullscreen": "escape", "mute": "m", "forward": "l", "back": "j",
               "skip_ad": ""}
ASK_S = 2.0                # longest wait for Windows' media list (it can hang)
SETTLE_S = 0.25            # between checks that play/pause really happened
ACTIONS = ["play", "pause", "toggle", "fullscreen", "exit_fullscreen", "mute", "forward", "back",
           "next", "previous", "skip_ad"]


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


class WinRTThread:
    """Everything that talks to Windows' media list runs on this one thread: its own asyncio loop,
    with COM set up as multithreaded. WinRT's answers can't reach a thread that some other part
    of the program has put in single-threaded COM mode (audio and UI libraries do that), and then
    an await never finishes: that froze Nova's window and the update's check (owner's case)."""

    def __init__(self):
        self.loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()

    def _start(self) -> None:
        ready = threading.Event()

        def run() -> None:
            if sys.platform == "win32":
                try:
                    import ctypes
                    ctypes.windll.ole32.CoInitializeEx(None, 0)          # COINIT_MULTITHREADED
                except Exception:
                    pass
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self.loop = loop
            ready.set()
            loop.run_forever()
        threading.Thread(target=run, name="nova-winrt", daemon=True).start()
        ready.wait(5)

    async def run(self, coro, timeout: float):
        with self._lock:
            if self.loop is None:
                self._start()
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        try:
            return await asyncio.wait_for(asyncio.wrap_future(fut), timeout)
        except asyncio.TimeoutError:
            fut.cancel()
            raise


WINRT = WinRTThread()


def apartment() -> str:
    """This thread's COM mode, for the log: 'STA' here is what makes WinRT awaits hang."""
    if sys.platform != "win32":
        return "n/a"
    import ctypes
    kind, qual = ctypes.c_int(), ctypes.c_int()
    hr = ctypes.windll.ole32.CoGetApartmentType(ctypes.byref(kind), ctypes.byref(qual))
    if hr != 0:
        return "not set up"
    return {0: "STA", 1: "MTA", 2: "NA", 3: "main STA"}.get(kind.value, str(kind.value))


class MediaBackend:
    """Windows' media sessions (GlobalSystemMediaTransportControls). A fake in tests."""

    STUCK_S = 60.0                  # after Windows' list stops answering, don't ask again for this long

    def __init__(self):
        self._sessions: list = []
        self._manager = None
        self._logged = False
        self._stuck_until = 0.0

    def stuck(self) -> bool:
        """Windows' media list recently didn't answer (so it isn't asked for a while)."""
        return time.monotonic() < self._stuck_until

    async def list(self) -> list[Media]:
        if sys.platform != "win32":
            return []
        if not self._logged:
            self._logged = True
            try:
                log.info("media list runs on its own thread (Nova's main thread COM mode: %s)", apartment())
            except Exception:
                pass
        if self.stuck():
            return []                               # it hung a moment ago: don't wait 4 s again
        try:
            return await WINRT.run(self._list(), ASK_S * 2)
        except ImportError as e:   # a winrt piece missing: carry on with keys and the media key
            log.warning("media sessions unavailable (%s): run scripts\\update.ps1", e)
            return []
        except asyncio.TimeoutError:
            # Windows can stop answering (one app's media controls stuck): never wait forever.
            log.warning("Windows' media list didn't answer within %.0f s; not asking again for %.0f s",
                        ASK_S * 2, self.STUCK_S)
            self._stuck_until = time.monotonic() + self.STUCK_S
            self._manager = None                    # ask Windows afresh next time
            return []

    async def _list(self) -> list[Media]:
        """On the WinRT thread."""
        from winrt.windows.media.control import (
            GlobalSystemMediaTransportControlsSessionManager as Manager)
        if self._manager is None:
            self._manager = await asyncio.wait_for(Manager.request_async(), ASK_S)
        self._sessions = list(self._manager.get_sessions())
        out = []
        for i, s in enumerate(self._sessions):
            try:
                props = await asyncio.wait_for(s.try_get_media_properties_async(), ASK_S)
                status = int(s.get_playback_info().playback_status)
            except Exception:                        # incl. a player that doesn't answer
                continue
            out.append(Media(i, s.source_app_user_model_id or "", (props.title if props else "") or "",
                             (props.artist if props else "") or "",
                             {4: "playing", 5: "paused", 3: "stopped"}.get(status, "other")))
        return out

    async def count(self) -> int | None:
        """How many players Windows lists, or None when it doesn't answer (for the doctor)."""
        async def go():
            from winrt.windows.media.control import (
                GlobalSystemMediaTransportControlsSessionManager as Manager)
            return len(list((await Manager.request_async()).get_sessions()))
        try:
            return await WINRT.run(go(), 5)
        except asyncio.TimeoutError:
            return None

    def available(self) -> bool:
        """Can Nova see what Windows is playing at all? (Windows, winrt installed.)"""
        if sys.platform != "win32":
            return False
        import importlib.util
        try:
            return importlib.util.find_spec("winrt.windows.media.control") is not None
        except ModuleNotFoundError:
            return False

    async def command(self, media: Media, action: str) -> bool:
        s = self._sessions[media.id]
        fn = {"play": s.try_play_async, "pause": s.try_pause_async,
              "toggle": s.try_toggle_play_pause_async, "next": s.try_skip_next_async,
              "previous": s.try_skip_previous_async}[action]

        async def go():
            return bool(await fn())
        try:
            return await WINRT.run(go(), ASK_S)
        except asyncio.TimeoutError:
            return False


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
                label = window.process.removesuffix(".exe").capitalize() or "the browser"
                if not await asyncio.to_thread(pc.WINDOWS.focus, window.hwnd):
                    raise ToolError(f"Windows wouldn't let me switch to {label}. Click on it once, then ask again.")
                await asyncio.sleep(0.25)
            if action in ("fullscreen", "exit_fullscreen"):
                done.append(await _fullscreen(window, action == "fullscreen", ctx))
                continue
            if action == "mute" and await _click_button(window, ("mute", "unmute"), (), ctx):
                done.append("mute toggled")
                continue
            if action == "skip_ad":
                # YouTube's own "Skip" / "Skip Ad" button; there's no key for it.
                skip = (("skip ad", "skip ads"), ("skip navigation",))
                if not await _click_button(window, *skip, ctx, exact="skip"):
                    raise ToolError("I can't see a Skip button yet. The ad may not be skippable, or not for a few seconds.")
                await asyncio.sleep(0.6)
                if await _has_button(window, *skip, exact="skip"):
                    raise ToolError("I clicked Skip, but the ad is still showing. Try again in a moment.")
                done.append("skipped the ad")
                continue
            if action in ("forward", "back"):
                done.append(await _seek(window, action, int(args.get("seconds") or 10)))
                continue
            await asyncio.to_thread(pc.WINDOWS.press, pc.KEYS[KEY_ACTIONS[action]])
            await asyncio.sleep(0.15)
            done.append("mute toggled")
            continue
        if media is None:
            if action in ("play", "pause"):
                # Windows' media list said nothing (it can get stuck): use the player itself. Its
                # Play/Pause button's name says what state it's in, before and after.
                if window is None:
                    window = await asyncio.to_thread(find_video_window, None)
                if window is not None:
                    done.append(await _play_by_button(window, action, ctx))
                    continue
            if action in ("play", "pause", "toggle"):
                from assistant.tools import music
                await asyncio.to_thread(music.press_media_key, "play_pause")
                done.append("I couldn't see the video, so I pressed the play/pause key")
                continue
            raise ToolError("I can't find a video playing on this PC.")
        if action == "play" and media.status == "playing":
            done.append("already playing")
            continue
        if action == "pause" and media.status == "paused":
            done.append("already paused")
            continue
        ok = await media_api.command(media, action)
        if action in ("play", "pause"):
            # "Accepted" isn't "happened" (owner: "he says he played the video but nothing
            # happens"). Check; if it didn't take, press the player's own button; else say so.
            want = "playing" if action == "play" else "paused"
            if not (ok and await _settled(media_api, media, want)):
                if window is None:
                    window = await asyncio.to_thread(find_video_window, media)
                if window is None or not await _player_button(window, action, ctx) \
                        or not await _settled(media_api, media, want):
                    raise ToolError(f"I tried, but the video didn't {'start' if action == 'play' else 'pause'}. "
                                    "Click on the video once, then ask again.")
            media.status = want
        elif not ok:
            raise ToolError(f"The video didn't accept '{action}'.")
        done.append({"play": "playing", "pause": "paused", "toggle": "play/pause pressed",
                     "next": "next video", "previous": "previous video"}[action])
    what = f": {media.title}" if media and media.title else ""
    text = ", ".join(done)
    return text[0].upper() + text[1:] + what + "."


async def _seek(window, action: str, seconds: int) -> str:
    """Skip ahead / back: on YouTube L/J jump 10 s and the arrow keys 5 s; elsewhere the arrows."""
    from assistant.tools import pc
    seconds = max(5, min(seconds, 600))
    youtube = "youtube" in window.title.lower()
    step, arrow = 10, "right" if action == "forward" else "left"
    if youtube:
        tens, fives = divmod(seconds, 10)
        presses = [KEY_ACTIONS[action]] * tens + [arrow] * (1 if fives >= 5 else 0)
    else:
        step = 5
        presses = [arrow] * max(1, round(seconds / step))
    for key in presses[:60]:
        await asyncio.to_thread(pc.WINDOWS.press, pc.KEYS[key])
        await asyncio.sleep(0.03)
    await asyncio.sleep(0.12)
    amount = f"{seconds // 60} minute{'s' if seconds >= 120 else ''}" if seconds % 60 == 0 else f"{seconds} seconds"
    return f"skipped ahead {amount}" if action == "forward" else f"went back {amount}"


async def ui_state(window) -> str | None:
    """'playing' / 'paused' from the player's own button (YouTube: 'Pause (k)' while playing,
    'Play (k)' while paused), or None when the page doesn't say."""
    from assistant.tools import uia
    try:
        elements = await asyncio.to_thread(uia.UIA.elements, window.hwnd)
    except Exception:
        return None
    for e in elements:
        if e.kind != "button":
            continue
        n = uia._norm(e.name)
        if n in ("pause", "pause k") or n.startswith("pause k "):
            return "playing"
        if n in ("play", "play k") or n.startswith("play k "):
            return "paused"
    return None


async def _play_by_button(window, action: str, ctx: ToolContext) -> str:
    from assistant.tools import pc
    want = "playing" if action == "play" else "paused"
    label = window.process.removesuffix(".exe").capitalize() or "the browser"
    before = await ui_state(window)
    if before == want:
        return f"already {'playing' if action == 'play' else 'paused'}"
    if not await asyncio.to_thread(pc.WINDOWS.focus, window.hwnd):
        raise ToolError(f"Windows wouldn't let me switch to {label}. Click on it once, then ask again.")
    await asyncio.sleep(0.2)
    await _player_button(window, action, ctx)
    for _ in range(8):
        await asyncio.sleep(max(SETTLE_S, 0.01))
        now = await ui_state(window)
        if now == want:
            return "playing" if action == "play" else "paused"
        if now is None and before is None:
            break
    if before is None:
        return f"pressed {action} in {label}, but the page doesn't show whether it worked"
    raise ToolError(f"I tried, but the video didn't {'start' if action == 'play' else 'pause'}. "
                    "Click on the video once, then ask again.")


async def _settled(api: MediaBackend, media: Media, want: str, tries: int = 6) -> bool:
    """Did the video really end up playing / paused? (Windows' list, re-read a few times.)"""
    for _ in range(tries):
        await asyncio.sleep(SETTLE_S)
        for m in await api.list():
            if m.app == media.app and m.title == media.title:
                if m.status == want:
                    return True
                break
        else:
            return False                               # the video is gone
    return False


async def _player_button(window, action: str, ctx: ToolContext) -> bool:
    """The player's own Play / Pause button (YouTube: "Play (k)"), else its key."""
    from assistant.tools import pc
    if not await asyncio.to_thread(pc.WINDOWS.focus, window.hwnd):
        return False
    await asyncio.sleep(0.2)
    if await _click_button(window, (f"{action} k",), ("playlist", "play all", "next", "previous"), ctx, exact=action):
        return True
    await asyncio.to_thread(pc.WINDOWS.press, pc.KEYS["k" if "youtube" in window.title.lower() else "space"])
    return True


async def _click_button(window, names: tuple[str, ...], avoid: tuple[str, ...], ctx: ToolContext,
                        exact: str | None = None) -> bool:
    """Click the player's own button (found by name, like 'Full screen (f)'). The F key only
    works when the page has the keyboard (not the search box), a button always does."""
    from assistant.tools import grid, uia
    try:
        elements = await asyncio.to_thread(uia.UIA.elements, window.hwnd)
    except Exception:
        return False
    for e in elements:
        n = uia._norm(e.name).replace("fullscreen", "full screen")
        if e.kind == "button" and (any(n.startswith(x) for x in names) or n == exact) \
                and not any(a in n for a in avoid):
            g = grid.controller(ctx)
            await asyncio.to_thread(g.mouse.move, e.rect.x + e.rect.w // 2, e.rect.y + e.rect.h // 2)
            await asyncio.to_thread(g.mouse.click, "left", False)
            return True
    return False


async def _has_button(window, names: tuple[str, ...], avoid: tuple[str, ...], exact: str | None = None) -> bool:
    from assistant.tools import uia
    try:
        elements = await asyncio.to_thread(uia.UIA.elements, window.hwnd)
    except Exception:
        return False
    return any(e.kind == "button" and (any(uia._norm(e.name).startswith(x) for x in names) or uia._norm(e.name) == exact)
               and not any(a in uia._norm(e.name) for a in avoid) for e in elements)


async def _fullscreen(window, want: bool, ctx: ToolContext) -> str:
    """Full screen on/off, checked afterwards: never claim it worked when it didn't."""
    from assistant.tools import pc

    def state() -> bool:
        return pc.WINDOWS.is_fullscreen(window.hwnd)
    if await asyncio.to_thread(state) == want:
        return "already full screen" if want else "not full screen"
    attempts = ([lambda: _click_button(window, ("full screen",), ("exit",), ctx), lambda: _press("f")]
                if want else [lambda: _press("escape"), lambda: _click_button(window, ("exit full screen",), (), ctx)])
    for attempt in attempts:
        if await attempt() is False:
            continue
        for _ in range(8):                                 # the page animates into full screen
            await asyncio.sleep(0.15)
            if await asyncio.to_thread(state) == want:
                return "full screen" if want else "out of full screen"
    raise ToolError("I tried, but the video didn't go full screen. Click on the video once, then ask again."
                    if want else "I couldn't get it out of full screen. Press Esc.")


async def _press(key: str) -> bool:
    from assistant.tools import pc
    await asyncio.to_thread(pc.WINDOWS.press, pc.KEYS[key])
    return True


def register(reg: ToolRegistry) -> None:
    register_media(reg)
    reg.tool("video", "Control the video playing in the browser (YouTube, Netflix, Twitch...) or a "
             "video app. Finds it by itself: no need to ask which video. actions run in order, "
             "e.g. ['fullscreen', 'play']. play/pause are exact (not toggles). skip_ad clicks YouTube's Skip button.",
             {"type": "object", "properties": {
                 "actions": {"type": "array", "items": {"type": "string", "enum": ACTIONS},
                             "minItems": 1, "maxItems": 5},
                 "seconds": {"type": "integer", "minimum": 5, "maximum": 600,
                             "description": "how far forward/back skips (default 10)"}},
              "required": ["actions"], "additionalProperties": False},
             risk=Risk.SAFE, category="media")(video)


# --- whatever is playing ------------------------------------------------------------------------
async def media(args: dict, ctx: ToolContext, _media: MediaBackend | None = None) -> str:
    """Pause/play/next/previous on whatever is actually playing: Spotify, YouTube, anything.
    (Plain 'pause' used to always mean Spotify, so a playing video 'wasn't playing'.)"""
    api = _media or MEDIA
    action = args["action"]
    items = await api.list()
    if not items:
        # Windows lists nothing (its media list can get stuck). Spotify can say what it's playing
        # itself, before and after; otherwise press the key and say it's unchecked. Never "Skipped."
        # on faith (owner: "you didn't skip anything").
        checked = await _spotify_checked(action, ctx)
        if checked is not None:
            return checked
        from assistant.tools import music
        key = {"play": "play_pause", "pause": "play_pause", "toggle": "play_pause"}.get(action, action)
        await asyncio.to_thread(music.press_media_key, key)
        what = {"next": "next-track", "previous": "previous-track"}.get(action, "play/pause")
        return (f"I pressed the {what} key, but Windows isn't telling me what's playing, "
                "so I can't check it worked.")
    playing = [m for m in items if m.status == "playing"]
    paused = [m for m in items if m.status == "paused"]
    if action == "pause":
        if not playing:
            return "Nothing is playing."
        target = playing[0]
    elif action == "play":
        if playing:
            return f"{playing[0].title or 'It'} is already playing."
        if not paused:
            return "There's nothing paused to resume."
        target = paused[0]
    else:
        target = (playing or paused or items)[0]
    ok = await api.command(target, action)
    if action in ("play", "pause"):
        want = "playing" if action == "play" else "paused"
        if not (ok and await _settled(api, target, want)):
            # Some players ignore Windows' request: the keyboard's play/pause key usually works.
            from assistant.tools import music
            await asyncio.to_thread(music.press_media_key, "play_pause")
            if not await _settled(api, target, want):
                raise ToolError(f"{_app_name(target)} didn't {'start' if action == 'play' else 'pause'}.")
    elif not ok:
        raise ToolError(f"{_app_name(target)} didn't accept that.")
    name = f" {target.title}" if target.title else ""
    return {"pause": f"Paused{name}.", "play": f"Playing{name}.", "next": "Next.",
            "previous": "Previous."}.get(action, "Done.")


async def _spotify_checked(action: str, ctx: ToolContext) -> str | None:
    """Do it through Spotify and check with Spotify. None when Spotify can't say (not linked,
    nothing on it, an error): the caller falls back."""
    from assistant.integrations.spotify import Spotify, SpotifyError
    from assistant.tools import music
    if action not in ("next", "previous", "play", "pause") or not Spotify.linked():
        return None
    sp = music._spotify(ctx)

    async def state():
        s = await sp._call("GET", "/me/player")
        return s if s and s.get("item") else None
    try:
        before = await state()
        if before is None:
            return None
        track = lambda s: (s["item"].get("id") or s["item"].get("uri"), s["item"].get("name", ""),  # noqa: E731
                           ", ".join(a["name"] for a in s["item"].get("artists", [])))
        was = track(before)
        if action == "pause" and not before.get("is_playing"):
            return f"Spotify is already paused on {was[1]}."
        if action == "play" and before.get("is_playing"):
            return f"{was[1]} is already playing."
        await sp.control({"play": "resume"}.get(action, action))
        for _ in range(12):
            await asyncio.sleep(max(SETTLE_S, 0.01))
            now = await state()
            if now is None:
                continue
            if action in ("next", "previous") and track(now)[0] != was[0]:
                name, by = track(now)[1:]
                return f"Now playing {name}" + (f" by {by}." if by else ".")
            if action == "pause" and not now.get("is_playing"):
                return f"Paused {was[1]}."
            if action == "play" and now.get("is_playing"):
                return f"Playing {track(now)[1]}."
    except SpotifyError:
        return None
    raise ToolError(f"I asked Spotify to {'skip' if action == 'next' else action}, but nothing changed.")


def _app_name(m: Media) -> str:
    a = m.app.lower()
    return "Spotify" if "spotify" in a else "The browser" if any(b in a for b in BROWSERS) else "That app"


def register_media(reg: ToolRegistry) -> None:
    reg.tool("media", "Pause, play (resume), next or previous on whatever is playing on the PC (Spotify, "
             "a YouTube video, any player). Use for plain 'pause' / 'resume' / 'skip'.",
             {"type": "object", "properties": {"action": {"type": "string", "enum": ["play", "pause", "next",
                                                                                      "previous"]}},
              "required": ["action"], "additionalProperties": False}, risk=Risk.SAFE, category="media")(media)
