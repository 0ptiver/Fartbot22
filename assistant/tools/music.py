"""Music: Spotify (Web API, needs a one-time link) and system media keys (any app, no setup)."""

from __future__ import annotations

import sys

from assistant.integrations.spotify import NotLinked, Spotify, SpotifyError
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


async def play_music(args: dict, ctx: ToolContext) -> str:
    try:
        return await _spotify(ctx).play(args.get("query") or None, args.get("kind", "auto"))
    except SpotifyError as e:
        raise ToolError(str(e)) from e


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
        risk=Risk.SAFE, category="music",
    )(media_key)
