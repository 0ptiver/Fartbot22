"""System tools: time, volume, open apps."""

from __future__ import annotations

import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from assistant.core.launch import launch
from assistant.tools.registry import Risk, ToolContext, ToolError, ToolRegistry

IS_WINDOWS = sys.platform == "win32"


# --- time ------------------------------------------------------------------
# Places people ask about whose time zone isn't named after them.
_ZONES = {"la": "America/Los_Angeles", "los angeles": "America/Los_Angeles", "california": "America/Los_Angeles",
          "san francisco": "America/Los_Angeles", "seattle": "America/Los_Angeles", "vegas": "America/Los_Angeles",
          "las vegas": "America/Los_Angeles", "texas": "America/Chicago", "dallas": "America/Chicago",
          "houston": "America/Chicago", "miami": "America/New_York", "florida": "America/New_York",
          "boston": "America/New_York", "washington": "America/New_York", "atlanta": "America/New_York",
          "england": "Europe/London", "uk": "Europe/London", "the uk": "Europe/London", "scotland": "Europe/London",
          "manchester": "Europe/London", "leeds": "Europe/London", "france": "Europe/Paris", "spain": "Europe/Madrid",
          "germany": "Europe/Berlin", "italy": "Europe/Rome", "japan": "Asia/Tokyo", "china": "Asia/Shanghai",
          "beijing": "Asia/Shanghai", "india": "Asia/Kolkata", "delhi": "Asia/Kolkata", "mumbai": "Asia/Kolkata",
          "dubai": "Asia/Dubai", "australia": "Australia/Sydney", "korea": "Asia/Seoul", "south korea": "Asia/Seoul",
          "brazil": "America/Sao_Paulo", "mexico": "America/Mexico_City", "canada": "America/Toronto",
          "hawaii": "Pacific/Honolulu", "new zealand": "Pacific/Auckland", "philippines": "Asia/Manila"}


def zone_for(place: str) -> ZoneInfo | None:
    """'tokyo' -> Asia/Tokyo, 'new york' -> America/New_York, 'LA' -> America/Los_Angeles."""
    from zoneinfo import available_timezones
    p = place.strip().lower().removeprefix("the ")
    if p in _ZONES:
        return ZoneInfo(_ZONES[p])
    want = p.replace(" ", "_")
    for name in sorted(available_timezones()):
        if "/" in name and name.rsplit("/", 1)[1].lower() == want:
            return ZoneInfo(name)
    return None


def get_time(args: dict, ctx: ToolContext) -> str:
    place = (args.get("place") or "").strip()
    here = ZoneInfo(ctx.settings.assistant.timezone)
    tz = zone_for(place) if place else here
    if tz is None:
        raise ToolError(f"I don't know the time zone for {place}.")
    now = datetime.now(tz)
    if place:
        diff = (now.utcoffset() - datetime.now(here).utcoffset()).total_seconds() / 3600
        rel = "the same as here" if diff == 0 else \
            f"{abs(diff):g} hour{'s' if abs(diff) != 1 else ''} {'ahead' if diff > 0 else 'behind'}"
        return f"It's {now.hour % 12 or 12}:{now:%M} {now:%p} on {now:%A} in {place.title()}, {rel}."
    # Short enough to be read out as it is (the fast path speaks it): "It's 1:26 PM, Sunday 27 September."
    return f"It's {now.hour % 12 or 12}:{now:%M} {now:%p}, {now:%A} {now.day} {now:%B}."


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
    """Start Menu (all users + mine) and both Desktops: Steam puts its games' shortcuts there."""
    dirs = []
    for var in ("ProgramData", "APPDATA"):
        base = os.environ.get(var)
        if base:
            dirs.append(Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
    for var in ("USERPROFILE", "PUBLIC"):
        base = os.environ.get(var)
        if base:
            dirs.append(Path(base) / "Desktop")
    return dirs


def _words(text: str) -> list[str]:
    return re.sub(r"[^a-z0-9 ]+", " ", text.lower().replace("&", " and ")).split()


def name_score(want: str, name: str) -> int | None:
    """How well a spoken app name matches a shortcut's name (lower is better, None = no match).
    'steam' = 'Steam'; 'gta' ~ 'Grand Theft Auto V' (initials); 'rockstar' ~ 'Rockstar Games Launcher'."""
    w, n = " ".join(_words(want)), " ".join(_words(name))
    if not w or not n:
        return None
    if n == w or n.replace(" ", "") == w.replace(" ", ""):
        return 0                                          # "five m" = "FiveM"
    if n.startswith(w) or n.replace(" ", "").startswith(w.replace(" ", "")):
        return 1
    if f" {w}" in f" {n}":
        return 2
    if all(x in n.split() for x in w.split()):
        return 3
    initials = "".join(x[0] for x in n.split())
    if len(w) >= 2 and " " not in w and initials.startswith(w):
        return 4
    return None


def find_shortcut(name: str, dirs: list[Path] | None = None) -> Path | None:
    """Find a Start Menu / Desktop shortcut (.lnk, or Steam's .url) whose name best matches `name`."""
    best: tuple[int, int, Path] | None = None
    for d in dirs if dirs is not None else _start_menu_dirs():
        if not d.exists():
            continue
        for lnk in [*d.rglob("*.lnk"), *d.rglob("*.url")]:
            stem = lnk.stem.lower()
            if "uninstall" in stem or "readme" in stem or "help" == stem:
                continue
            score = name_score(name, lnk.stem)
            if score is not None and (best is None or (score, len(stem)) < best[:2]):
                best = (score, len(stem), lnk)
    return best[2] if best else None


_GAME_LINKS = ("steam://", "com.epicgames.launcher://", "uplay://", "origin://", "origin2://", "battlenet://")
_KNOWN_GAMES = {"fivem", "five m", "gta", "gta 5", "gta v", "gta five", "minecraft", "roblox", "fortnite", "valorant",
                "league of legends", "rocket league", "apex", "apex legends", "counter strike", "cs2", "call of duty",
                "warzone", "overwatch", "rust", "red dead", "red dead redemption", "red dead redemption 2"}
_GAMES_CACHE: tuple[float, list[Path]] = (0.0, [])


def game_shortcuts(dirs: list[Path] | None = None) -> list[Path]:
    """Store-launcher game shortcuts (Steam, Epic, Ubisoft, EA, Battle.net) on the Start menu and desktops."""
    global _GAMES_CACHE
    if dirs is None and time.monotonic() - _GAMES_CACHE[0] < 60:
        return _GAMES_CACHE[1]
    found = []
    for d in dirs if dirs is not None else _start_menu_dirs():
        if d.exists():
            for f in d.rglob("*.url"):
                try:
                    if any(x in f.read_text(errors="ignore")[:2000].lower() for x in _GAME_LINKS):
                        found.append(f)
                except OSError:
                    continue
    if dirs is None:
        _GAMES_CACHE = (time.monotonic(), found)
    return found


def is_game(name: str, dirs: list[Path] | None = None) -> bool:
    """'play gta' is the game, not a song called 'gta'."""
    n = " ".join(_words(name))
    if n in _KNOWN_GAMES:
        return True
    return any((sc := name_score(name, f.stem)) is not None and sc <= 4 for f in game_shortcuts(dirs)) if len(n) >= 2 else False


_APPS_CACHE: tuple[float, list[tuple[str, str]]] = (0.0, [])


def start_apps() -> list[tuple[str, str]]:
    """Every app in the Start menu, Store apps too (Xbox, WhatsApp...): (name, AppUserModelID).
    Windows only; read once every 10 minutes (PowerShell takes about a second)."""
    global _APPS_CACHE
    if not IS_WINDOWS:
        return []
    if time.monotonic() - _APPS_CACHE[0] < 600:
        return _APPS_CACHE[1]
    import json
    import subprocess
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                              "Get-StartApps | Select-Object Name, AppID | ConvertTo-Json -Compress"],
                             capture_output=True, text=True, timeout=8,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        rows = json.loads(out or "[]")
        rows = [rows] if isinstance(rows, dict) else rows
        apps = [(r["Name"], r["AppID"]) for r in rows if r.get("Name") and r.get("AppID")]
    except (OSError, ValueError, subprocess.SubprocessError):
        apps = []
    _APPS_CACHE = (time.monotonic(), apps)
    return apps


def find_start_app(name: str, apps: list[tuple[str, str]] | None = None) -> tuple[str, str] | None:
    best = None
    for app_name, app_id in (apps if apps is not None else start_apps()):
        if "uninstall" in app_name.lower():
            continue
        score = name_score(name, app_name)
        if score is not None and (best is None or (score, len(app_name)) < best[:2]):
            best = (score, len(app_name), app_name, app_id)
    return (best[2], best[3]) if best else None


OPEN_WAIT_S = 4.0          # how long to watch for the app's window before saying "starting"
_GENERIC = {"microsoft", "games", "game", "launcher", "app", "the", "for", "and", "of", "desktop", "client"}


def _app_windows(label: str) -> set[int]:
    """Windows that look like they belong to this app (title or process name)."""
    from assistant.tools import pc
    words = [w for w in _words(label) if w not in _GENERIC and len(w) > 1] or _words(label)
    found = set()
    for w in pc.WINDOWS.list():
        text = " ".join(_words(f"{w.title} {w.process.removesuffix('.exe')}")).split()
        if any(x in text for x in words):
            found.add(w.hwnd)
    return found


def _launch(target: str) -> None:
    launch(target)


def open_app(args: dict, ctx: ToolContext, _launcher=_launch, _dirs=None, _apps=None) -> str:
    name = args["name"].strip()
    aliases = {k.lower(): v for k, v in ctx.settings.tools.app_aliases.items()}
    target = aliases.get(name.lower())
    label = name
    if target is None:
        lnk = find_shortcut(name, _dirs)
        lnk_score = name_score(name, lnk.stem) if lnk else None
        app = find_start_app(name, _apps) if lnk_score != 0 else None
        if app and (lnk is None or name_score(name, app[0]) < lnk_score):
            label, target = app[0], f"shell:AppsFolder\\{app[1]}"
        elif lnk is not None:
            label, target = lnk.stem, str(lnk)
        else:
            raise ToolError(f"I couldn't find an app called '{name}' on this PC.")
    try:
        before = _app_windows(label)
    except Exception:
        before = None                                     # can't look at windows: just open it
    _launcher(target)
    if before is None:
        return f"Opened {label}."
    # Say "opened" only when its window really shows up (the launcher saying OK isn't proof).
    deadline = time.monotonic() + OPEN_WAIT_S
    while True:
        if _app_windows(label) - before:
            return f"Opened {label}."
        if time.monotonic() >= deadline:
            break
        time.sleep(0.25)
    if before:
        return f"{label} is already open."
    return f"Starting {label}. It can take a moment to appear."


# --- brightness (the laptop's own screen) ----------------------------------------------------
class Brightness:
    """The built-in screen's brightness through Windows' WMI (external monitors don't support it).
    A fake in tests."""

    def _ps(self, script: str) -> str:
        import subprocess
        return subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                              capture_output=True, text=True, timeout=8,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.strip()

    def get(self) -> int | None:
        if not IS_WINDOWS:
            return None
        out = self._ps("(Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightness "
                       "-ErrorAction SilentlyContinue | Select-Object -First 1).CurrentBrightness")
        return int(out) if out.isdigit() else None

    def set(self, level: int) -> None:
        self._ps("Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightnessMethods | "
                 f"Invoke-CimMethod -MethodName WmiSetBrightness -Arguments @{{Timeout=1; Brightness={int(level)}}}")


BRIGHTNESS = Brightness()


def brightness(args: dict, ctx: ToolContext) -> str:
    now = BRIGHTNESS.get()
    if now is None:
        raise ToolError("I can't change this screen's brightness (only the laptop's own screen supports it).")
    action = args.get("action", "get")
    if action == "get":
        return f"Brightness is {now} percent."
    step = int(args.get("amount") or 20)
    want = {"set": args.get("level", now), "up": now + step, "down": now - step}[action]
    want = max(0 if action == "set" else 5, min(100, int(want)))
    BRIGHTNESS.set(want)
    got = BRIGHTNESS.get()
    if got is None or abs(got - want) > 5:
        raise ToolError(f"I asked for {want} percent, but the brightness is still {now}.")
    return f"Brightness {got} percent."


def register(reg: ToolRegistry) -> None:
    reg.tool("brightness", "The laptop screen's brightness: get, set (level 0-100), up or down.",
             {"type": "object", "properties": {
                 "action": {"type": "string", "enum": ["get", "set", "up", "down"]},
                 "level": {"type": "integer", "minimum": 0, "maximum": 100},
                 "amount": {"type": "integer", "minimum": 1, "maximum": 100}},
              "required": ["action"], "additionalProperties": False}, risk=Risk.SAFE, category="system")(brightness)
    reg.tool(
        "get_time",
        "Get the current local date and time, or the time in another place (place='Tokyo').",
        {"type": "object", "properties": {"place": {"type": "string", "maxLength": 60}},
         "additionalProperties": False},
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
        "Open an application or game by name (e.g. 'spotify', 'discord', 'steam', 'gta'). "
        "Looks up aliases, Start Menu and desktop shortcuts, and Store apps.",
        {
            "type": "object",
            "properties": {"name": {"type": "string", "minLength": 1, "maxLength": 100}},
            "required": ["name"],
            "additionalProperties": False,
        },
        risk=Risk.SAFE, category="apps",
    )(open_app)
