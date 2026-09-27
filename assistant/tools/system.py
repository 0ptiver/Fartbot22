"""System tools: time, volume, open apps."""

from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from assistant.core.launch import launch
from assistant.tools.registry import Risk, ToolContext, ToolError, ToolRegistry

IS_WINDOWS = sys.platform == "win32"


# --- time ------------------------------------------------------------------
def get_time(args: dict, ctx: ToolContext) -> str:
    tz = ZoneInfo(ctx.settings.assistant.timezone)
    now = datetime.now(tz)
    return now.strftime(f"%A %d %B %Y, %I:%M %p ({ctx.settings.assistant.timezone})")


# --- volume ----------------------------------------------------------------
def _endpoint_volume():
    """Return the IAudioEndpointVolume for the default speakers (Windows only)."""
    if not IS_WINDOWS:
        raise ToolError("Volume control is only implemented on Windows.")
    import comtypes
    from pycaw.pycaw import AudioUtilities

    comtypes.CoInitialize()  # handlers run in worker threads; COM needs per-thread init
    speakers = AudioUtilities.GetSpeakers()
    if hasattr(speakers, "EndpointVolume"):  # pycaw >= 20240210
        return speakers.EndpointVolume
    from ctypes import POINTER, cast

    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import IAudioEndpointVolume

    iface = speakers.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    return cast(iface, POINTER(IAudioEndpointVolume))


def volume(args: dict, ctx: ToolContext, _ep=None) -> str:
    ep = _ep or _endpoint_volume()
    action = args["action"]
    current = round(ep.GetMasterVolumeLevelScalar() * 100)
    step = args.get("amount", 10)
    if action == "get":
        muted = bool(ep.GetMute())
        return f"Volume is {current}%" + (" (muted)" if muted else "")
    if action == "mute":
        ep.SetMute(1, None)
        return "Muted."
    if action == "unmute":
        ep.SetMute(0, None)
        return f"Unmuted. Volume is {current}%."
    if action == "set":
        if "level" not in args:
            raise ToolError("'level' is required for action=set")
        target = args["level"]
    elif action == "up":
        target = current + step
    else:  # down
        target = current - step
    target = max(0, min(100, int(target)))
    ep.SetMasterVolumeLevelScalar(target / 100, None)
    if target > 0 and ep.GetMute():
        ep.SetMute(0, None)
    return f"Volume set to {target}%."


# --- open app --------------------------------------------------------------
def _start_menu_dirs() -> list[Path]:
    dirs = []
    for var in ("ProgramData", "APPDATA"):
        base = os.environ.get(var)
        if base:
            dirs.append(Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
    return dirs


def find_shortcut(name: str, dirs: list[Path] | None = None) -> Path | None:
    """Find a Start Menu shortcut whose name best matches `name`."""
    want = name.lower().strip()
    best: tuple[int, Path] | None = None
    for d in dirs if dirs is not None else _start_menu_dirs():
        if not d.exists():
            continue
        for lnk in d.rglob("*.lnk"):
            stem = lnk.stem.lower()
            if "uninstall" in stem:
                continue
            if stem == want:
                score = 0
            elif stem.startswith(want):
                score = 1
            elif want in stem:
                score = 2
            else:
                continue
            if best is None or (score, len(stem)) < (best[0], len(best[1].stem)):
                best = (score, lnk)
    return best[1] if best else None


def _launch(target: str) -> None:
    launch(target)


def open_app(args: dict, ctx: ToolContext, _launcher=_launch, _dirs=None) -> str:
    name = args["name"].strip()
    aliases = {k.lower(): v for k, v in ctx.settings.tools.app_aliases.items()}
    target = aliases.get(name.lower())
    if target is None:
        lnk = find_shortcut(name, _dirs)
        if lnk is None:
            raise ToolError(f"I couldn't find an app called '{name}' in the Start Menu or aliases.")
        target = str(lnk)
        label = lnk.stem
    else:
        label = name
    _launcher(target)
    return f"Opened {label}."


def register(reg: ToolRegistry) -> None:
    reg.tool(
        "get_time",
        "Get the current local date and time.",
        risk=Risk.SAFE, category="system",
    )(get_time)
    reg.tool(
        "volume",
        "Get or change the PC's master volume. Actions: get, set (needs level 0-100), "
        "up/down (by amount, default 10), mute, unmute.",
        {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["get", "set", "up", "down", "mute", "unmute"]},
                "level": {"type": "integer", "minimum": 0, "maximum": 100},
                "amount": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
        risk=Risk.SAFE, category="system",
    )(volume)
    reg.tool(
        "open_app",
        "Open an application by name (e.g. 'spotify', 'discord', 'notepad', 'steam'). "
        "Looks up configured aliases, then Start Menu shortcuts.",
        {
            "type": "object",
            "properties": {"name": {"type": "string", "minLength": 1, "maxLength": 100}},
            "required": ["name"],
            "additionalProperties": False,
        },
        risk=Risk.SAFE, category="apps",
    )(open_app)
