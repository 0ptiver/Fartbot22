"""Music: Spotify (Web API, needs a one-time link) and system media keys (any app, no setup)."""

from __future__ import annotations

import sys

from assistant.integrations.spotify import NotLinked, NotPlaying, Spotify, SpotifyError
from assistant.tools.registry import Risk, ToolContext, ToolError, ToolRegistry

_VK = {"play_pause": 0xB3, "next": 0xB0, "previous": 0xB1, "stop": 0xB2, "mute": 0xAD}


def press_media_key(action: str) -> None:
    """Send a media key (like the keyboard's play/pause button). Sends a key; doesn't hook one."""
    if sys.platform != "win32":
        raise ToolError("Media keys are only implemented on Windows.")
    import ctypes

    vk = _VK[action]
    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    user32.keybd_event(vk, 0, 0x1, 0)          # KEYEVENTF_EXTENDEDKEY
    user32.keybd_event(vk, 0, 0x1 | 0x2, 0)    # + KEYEVENTF_KEYUP


def _spotify(ctx: ToolContext) -> Spotify:
    sp = ctx.services.get("spotify")
    if sp is None:
        def open_app() -> None:
            if sys.platform == "win32":
                from assistant.core.launch import launch
                launch("spotify:")
        sp = ctx.services["spotify"] = Spotify(open_app=open_app)
    return sp


HEAR_TRIES = 8          # checks of what Windows says is playing (HEAR_S apart)
HEAR_S = 0.4


async def play_music(args: dict, ctx: ToolContext) -> str:
    """Play on Spotify, and only say so once this PC is really playing it.

    Owner: "he keeps saying I'm playing this on Spotify but nothing is happening". Spotify's
    servers say "OK" (and even "playing") while the app stays silent. So: ask Spotify, then
    check what Windows says is playing (the volume pop-up's list). Not playing? Open it in the
    Spotify app and press its own Play button, check again, and otherwise say it didn't work."""
    sp = _spotify(ctx)
    failure: SpotifyError | None = None
    said = None
    try:
        said = await sp.play(args.get("query") or None, args.get("kind", "auto"))
    except NotPlaying as e:
        if not getattr(sp, "last_target", None):
            raise ToolError(str(e)) from e
        failure = e                                        # try the app itself below
    except SpotifyError as e:
        raise ToolError(str(e)) from e                    # not linked, nothing found, Premium...
    target = sp.last_target or {"label": "your music", "title": None, "uri": None}
    heard = await hear_spotify(target)
    if heard or (heard is None and failure is None):
        return said or f"Playing {target['label']}."
    if await play_in_app(target, ctx) and await hear_spotify(target):
        return f"Playing {target['label']}."
    why = f" ({failure})" if failure else ""
    raise ToolError(f"Spotify didn't start playing {target['label']}{why}. Is the Spotify app open and "
                    "signed in? Pressing play in Spotify once usually wakes it up.")


async def hear_spotify(target: dict) -> bool | None:
    """Is this PC's Spotify playing what we asked for? None = Nova can't see (not Windows)."""
    import asyncio

    from assistant.tools import video
    if not video.MEDIA.available():
        return None
    want = _simple(target.get("title") or "")
    for _ in range(HEAR_TRIES):
        for m in await video.MEDIA.list():
            if m.is_music_app and m.status == "playing" and (not want or want in _simple(m.title)
                                                              or _simple(m.title) in want):
                return True
        await asyncio.sleep(HEAR_S)
    return False


async def play_in_app(target: dict, ctx: ToolContext) -> bool:
    """Open the song/playlist in the Spotify app and click its big green Play button (found by
    name with UI Automation, like a person would). True if it clicked something."""
    import asyncio

    from assistant.tools import grid, pc, uia
    if sys.platform != "win32" or not target.get("uri"):
        return False
    from assistant.core.launch import launch
    await asyncio.to_thread(launch, target["uri"])
    for _ in range(10):                                   # the app opens / navigates
        await asyncio.sleep(0.5)
        win = next((w for w in await asyncio.to_thread(pc.WINDOWS.list)
                    if w.process.lower().removesuffix(".exe") == "spotify"), None)
        if win is None:
            continue
        await asyncio.to_thread(pc.WINDOWS.focus, win.hwnd)
        try:
            elements = await asyncio.to_thread(uia.UIA.elements, win.hwnd)
        except Exception:
            continue
        button = pick_play_button(elements, target.get("title"), win)
        if button is None:
            continue
        g = grid.controller(ctx)
        await asyncio.to_thread(g.mouse.move, button.rect.x + button.rect.w // 2, button.rect.y + button.rect.h // 2)
        await asyncio.to_thread(g.mouse.click, "left", False)
        return True
    return False


def pick_play_button(elements, title: str | None, win):
    """The page's Play button: "Play <title> ..." if there is one, else the topmost plain
    "Play" above the player bar at the bottom (that one would resume the old song)."""
    from assistant.tools import uia, pc
    buttons = [e for e in elements if e.kind == "button"]
    if title:
        t = uia._norm(title)
        for e in buttons:
            if uia._norm(e.name).startswith(f"play {t}"):
                return e
    try:
        left, top, w, h = pc.WINDOWS.rect(win.hwnd)
    except Exception:
        top, h = 0, 10 ** 6
    plain = [e for e in buttons if uia._norm(e.name) == "play" and e.rect.y + e.rect.h < top + 0.82 * h]
    return min(plain, key=lambda e: e.rect.y) if plain else None


def _simple(text: str) -> str:
    import re
    return " ".join(re.sub(r"[^\w\s]", " ", (text or "").lower()).split())


async def music_control(args: dict, ctx: ToolContext) -> str:
    action = args["action"]
    if Spotify.linked():
        try:
            return await _spotify(ctx).control(action, args.get("level"))
        except NotLinked:
            pass
        except SpotifyError as e:
            raise ToolError(str(e)) from e
    # Not linked: media keys cover the basics for Spotify, YouTube, anything.
    keys = {"pause": "play_pause", "resume": "play_pause", "next": "next", "previous": "previous"}
    if action not in keys:
        raise ToolError("That needs Spotify linked. Run: python -m assistant spotify login")
    press_media_key(keys[action])
    return {"pause": "Paused.", "resume": "Playing.", "next": "Skipped.", "previous": "Going back."}[action]


async def now_playing(args: dict, ctx: ToolContext) -> str:
    try:
        return await _spotify(ctx).now_playing()
    except SpotifyError as e:
        raise ToolError(str(e)) from e


async def queue_song(args: dict, ctx: ToolContext) -> str:
    try:
        return await _spotify(ctx).queue(args["query"])
    except SpotifyError as e:
        raise ToolError(str(e)) from e


def media_key(args: dict, ctx: ToolContext) -> str:
    press_media_key(args["action"])
    return "Done."


def register(reg: ToolRegistry) -> None:
    reg.tool(
        "play_music",
        "Play music on Spotify. query = song, artist, album or playlist name. kind: 'auto' (default), "
        "'track', 'artist', 'album', 'playlist', 'liked_songs' (the user's liked songs), or "
        "'recently_played' (the last song they listened to). No query and kind auto = resume.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "maxLength": 200},
                "kind": {"type": "string", "enum": ["auto", "track", "artist", "album", "playlist",
                                                    "liked_songs", "recently_played"]},
            },
            "additionalProperties": False,
        },
        risk=Risk.SAFE, category="music",
    )(play_music)
    reg.tool(
        "music_control",
        "Control music playback: pause, resume, next, previous, shuffle_on, shuffle_off, "
        "repeat_on, repeat_off, or volume (Spotify's own volume, level 0-100).",
        {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["pause", "resume", "next", "previous", "shuffle_on",
                                                      "shuffle_off", "repeat_on", "repeat_off", "volume"]},
                "level": {"type": "integer", "minimum": 0, "maximum": 100},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
        risk=Risk.SAFE, category="music",
    )(music_control)
    reg.tool("now_playing", "What song is playing on Spotify right now.",
             risk=Risk.SAFE, category="music")(now_playing)
    reg.tool(
        "queue_song",
        "Add a song to the Spotify queue (plays after the current one).",
        {"type": "object", "properties": {"query": {"type": "string", "minLength": 1, "maxLength": 200}},
         "required": ["query"], "additionalProperties": False},
        risk=Risk.SAFE, category="music",
    )(queue_song)
    reg.tool(
        "media_key",
        "Press a media key for whatever app is playing (YouTube, a game, any player): play_pause, "
        "next, previous, stop, mute. Prefer music_control for Spotify.",
        {"type": "object", "properties": {"action": {"type": "string", "enum": list(_VK)}},
         "required": ["action"], "additionalProperties": False},
        # Hidden from the model: a blind toggle. Asked to pause the video and play a song, it
        # pressed it and restarted the video (owner's case). The `media` tool is exact.
        risk=Risk.SAFE, category="music", internal=True,
    )(media_key)
