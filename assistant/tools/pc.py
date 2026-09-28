"""PC control: system status, lock, power (confirm), windows, websites.

Windows calls go through small backend functions so the logic is testable anywhere.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from urllib.parse import quote_plus, urlparse

from assistant.tools.registry import Risk, ToolContext, ToolError, ToolRegistry

IS_WINDOWS = sys.platform == "win32"


# --- system status --------------------------------------------------------------------------
def gpu_stats() -> dict | None:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        out = subprocess.run(
            [exe, "--query-gpu=name,temperature.gpu,utilization.gpu,memory.used,memory.total",
             "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5,
            **({"creationflags": 0x08000000} if IS_WINDOWS else {})).stdout.strip().splitlines()[0]
        name, temp, util, used, total = [x.strip() for x in out.split(",")]
        return {"name": name, "temp": int(temp), "util": int(util), "mem_used": int(used), "mem_total": int(total)}
    except Exception:
        return None


def system_status(args: dict, ctx: ToolContext, _gpu=gpu_stats) -> str:
    import psutil

    what = args.get("what", "overview")
    parts = []
    if what in ("overview", "cpu"):
        parts.append(f"CPU {psutil.cpu_percent(interval=0.3):.0f}% busy")
    if what in ("overview", "memory"):
        vm = psutil.virtual_memory()
        parts.append(f"memory {vm.percent:.0f}% used ({vm.used / 2**30:.1f} of {vm.total / 2**30:.0f} GB)")
    if what in ("overview", "gpu"):
        g = _gpu()
        if g:
            parts.append(f"GPU {g['temp']}°C, {g['util']}% busy, "
                         f"{g['mem_used'] / 1024:.1f} of {g['mem_total'] / 1024:.0f} GB video memory")
        elif what == "gpu":
            parts.append("I can't read the GPU (no NVIDIA driver tools found)")
    if what in ("overview", "battery"):
        b = psutil.sensors_battery()
        if b:
            state = "charging" if b.power_plugged else "on battery"
            left = "" if b.power_plugged or b.secsleft in (psutil.POWER_TIME_UNLIMITED, psutil.POWER_TIME_UNKNOWN) \
                else f", about {b.secsleft // 3600}h {b.secsleft % 3600 // 60}m left"
            parts.append(f"battery {b.percent:.0f}% {state}{left}")
        elif what == "battery":
            parts.append("there's no battery reading")
    if what in ("overview", "disk"):
        for part in psutil.disk_partitions(all=False):
            if "cdrom" in part.opts or not part.fstype:
                continue
            try:
                u = psutil.disk_usage(part.mountpoint)
            except OSError:
                continue
            parts.append(f"drive {part.mountpoint.rstrip(chr(92))} {u.free / 2**30:.0f} GB free of {u.total / 2**30:.0f} GB")
            if what == "overview":
                break
    if what == "uptime":
        secs = time.time() - psutil.boot_time()
        parts.append(f"on for {int(secs // 3600)} hours {int(secs % 3600 // 60)} minutes")
    if not parts:
        raise ToolError("I couldn't read that.")
    text = "; ".join(parts)
    return text[0].upper() + text[1:] + "."


# --- lock / power ----------------------------------------------------------------------------
def _need_windows() -> None:
    if not IS_WINDOWS:
        raise ToolError("That only works on Windows.")


def _win_lock() -> None:
    _need_windows()
    import ctypes
    ctypes.windll.user32.LockWorkStation()  # type: ignore[attr-defined]


def _win_sleep() -> None:
    _need_windows()
    import ctypes
    # SetSuspendState(hibernate=False, force=True, disable_wake_events=False)
    ctypes.windll.powrprof.SetSuspendState(0, 1, 0)  # type: ignore[attr-defined]


def _shutdown_cmd(args: list[str]) -> None:
    _need_windows()
    subprocess.run(["shutdown", *args], check=False, capture_output=True,
                   **({"creationflags": 0x08000000} if IS_WINDOWS else {}))


@dataclass
class PowerBackend:
    lock = staticmethod(_win_lock)
    sleep = staticmethod(_win_sleep)
    shutdown = staticmethod(_shutdown_cmd)


POWER = PowerBackend()


def lock_pc(args: dict, ctx: ToolContext) -> str:
    POWER.lock()
    return "Locked."


POWER_DELAY_S = 60


def power(args: dict, ctx: ToolContext) -> str:
    action = args["action"]
    if action == "sleep":
        POWER.sleep()
        return "Going to sleep."
    flag = {"restart": "/r", "shutdown": "/s"}[action]
    POWER.shutdown([flag, "/t", str(POWER_DELAY_S), "/c", "Nova: say 'Nova, cancel the shutdown' to stop."])
    return f"{action.capitalize()} in {POWER_DELAY_S} seconds. Say 'cancel the shutdown' to stop it."


def cancel_shutdown(args: dict, ctx: ToolContext) -> str:
    POWER.shutdown(["/a"])
    return "Shutdown cancelled."


# --- windows --------------------------------------------------------------------------------
@dataclass
class Win:
    hwnd: int
    title: str
    process: str


WS_CAPTION, WS_THICKFRAME = 0x00C00000, 0x00040000


def fullscreen_style(style: int) -> bool:
    """No title bar and no resize border: how browsers and games look in full screen."""
    return (style & WS_CAPTION) != WS_CAPTION and not style & WS_THICKFRAME


class WindowBackend:
    """Win32 via ctypes. Replaced by a fake in tests."""

    def list(self) -> list[Win]:
        _need_windows()
        import ctypes
        from ctypes import wintypes

        import psutil

        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        found: list[Win] = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def cb(hwnd, _):
            if (user32.IsWindowVisible(hwnd) and user32.GetWindowTextLengthW(hwnd) > 0
                    and not user32.GetWindow(hwnd, 4)):         # skip owned popups (GW_OWNER)
                buf = ctypes.create_unicode_buffer(512)
                user32.GetWindowTextW(hwnd, buf, 512)
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                try:
                    proc = psutil.Process(pid.value).name()
                except Exception:
                    proc = ""
                if buf.value not in ("Program Manager", "Windows Input Experience"):
                    found.append(Win(int(hwnd or 0), buf.value, proc))
            return True

        user32.EnumWindows(cb, 0)
        return found

    def show(self, hwnd: int, how: str) -> None:
        _need_windows()
        import ctypes
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        codes = {"minimize": 6, "maximize": 3, "restore": 9}
        if how == "close":
            user32.PostMessageW(hwnd, 0x0010, 0, 0)            # WM_CLOSE: the app can ask to save
            return
        if how == "focus":
            self.focus(hwnd)
            return
        user32.ShowWindow(hwnd, codes[how])

    def focus(self, hwnd: int) -> bool:
        """Bring a window to the front and give it the keyboard. Windows normally refuses this
        to background programs (owner's case: 'full screen my video' pressed F in the wrong
        window), so: tap Alt, borrow the foreground window's input queue, and as a last resort
        minimise/restore. True if it really is in front afterwards."""
        _need_windows()
        import ctypes
        import time as _t
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        if user32.GetForegroundWindow() == hwnd:
            return True
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)
        fg = user32.GetForegroundWindow()
        me, them = k32.GetCurrentThreadId(), user32.GetWindowThreadProcessId(fg, None)
        attached = bool(them and them != me and user32.AttachThreadInput(me, them, True))
        try:
            user32.keybd_event(0x12, 0, 0, 0)                  # Alt tap: allows a focus change
            user32.keybd_event(0x12, 0, 2, 0)
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
            user32.SetFocus(hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(me, them, False)
        _t.sleep(0.08)
        if user32.GetForegroundWindow() != hwnd:
            user32.ShowWindow(hwnd, 6)                         # minimise + restore: always activates
            user32.ShowWindow(hwnd, 9)
            user32.SetForegroundWindow(hwnd)
            _t.sleep(0.15)
        return user32.GetForegroundWindow() == hwnd

    def foreground(self) -> int:
        _need_windows()
        import ctypes
        return int(ctypes.windll.user32.GetForegroundWindow() or 0)  # type: ignore[attr-defined]

    def is_fullscreen(self, hwnd: int) -> bool:
        """Does the window cover its whole screen (a video in full screen, a game)?"""
        _need_windows()
        import ctypes
        from ctypes import wintypes

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        left, top, w, h = self.rect(hwnd)
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        if not user32.GetMonitorInfoW(user32.MonitorFromWindow(hwnd, 2), ctypes.byref(info)):
            return False
        m = info.rcMonitor
        covers = left <= m.left and top <= m.top and left + w >= m.right and top + h >= m.bottom
        # A maximised window covers the whole screen too when that screen has no taskbar (the
        # owner's second monitor), which made Nova say "already full screen" and do nothing.
        # Real full screen (a video, a game) also drops the title bar and resize border.
        style = user32.GetWindowLongW(hwnd, -16)                          # GWL_STYLE
        return covers and fullscreen_style(style)

    def active(self) -> Win | None:
        """The window the user is working in: the foreground one, unless that's Nova's own
        window, then the next normal window down (skipping always-on-top overlays)."""
        _need_windows()
        import ctypes
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        fg = int(user32.GetForegroundWindow() or 0)
        wins = self.list()
        for w in wins:
            if w.hwnd == fg and not is_nova_window(w):
                return w
        for w in wins:
            if not is_nova_window(w) and not user32.GetWindowLongW(w.hwnd, -20) & 0x8:   # WS_EX_TOPMOST
                return w
        return None

    def rect(self, hwnd: int) -> tuple[int, int, int, int]:
        """(left, top, width, height) of a window, in real pixels."""
        _need_windows()
        import ctypes
        from ctypes import wintypes
        from assistant.tools.grid import _dpi_aware
        _dpi_aware()
        r = wintypes.RECT()
        ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r))  # type: ignore[attr-defined]
        return r.left, r.top, r.right - r.left, r.bottom - r.top

    def at(self, x: int, y: int) -> Win | None:
        """The top-level window under a point on the screen."""
        _need_windows()
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        hwnd = user32.WindowFromPoint(wintypes.POINT(x, y))
        root = user32.GetAncestor(hwnd, 2) if hwnd else 0              # GA_ROOT
        for w in self.list():
            if w.hwnd == root:
                return w
        return None

    def press(self, vk: int) -> None:
        _need_windows()
        import ctypes
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        user32.keybd_event(vk, 0, 0, 0)
        user32.keybd_event(vk, 0, 2, 0)

    def show_desktop(self) -> None:
        _need_windows()
        import ctypes
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        user32.keybd_event(0x5B, 0, 0, 0)       # Win
        user32.keybd_event(0x44, 0, 0, 0)       # D
        user32.keybd_event(0x44, 0, 2, 0)
        user32.keybd_event(0x5B, 0, 2, 0)


WINDOWS = WindowBackend()
THIS = ("this", "it", "that", "current", "active", "this window", "the window", "that window",
        "current window", "active window", "this app", "the app")


def is_nova_window(w: Win) -> bool:
    return w.title.strip().lower() == "nova"        # the HUD


def active_window() -> Win:
    w = WINDOWS.active()
    if w is None:
        raise ToolError("I can't tell which window you're using.")
    return w


def user_window_forward() -> Win | None:
    """Before typing or pressing keys: if Nova's own window is in front (you just clicked it),
    give the keyboard back to the window you were working in."""
    w = WINDOWS.active()
    if w is not None and hasattr(WINDOWS, "foreground") and WINDOWS.foreground() != w.hwnd:
        WINDOWS.focus(w.hwnd)
    return w


GAME_NAMES = ("the game", "game", "my game", "gta", "the gta")
BACK_NAMES = ("back", "previous", "the previous window", "the last window", "last window", "the last app",
              "what i was doing", "where i was")
_GAME_PLACES = ("steamapps\\common", "epic games", "rockstar games", "fivem", "riot games", "battle.net",
                "ubisoft game launcher\\games", "ea games", "xboxgames", "gog galaxy\\games")


def game_processes() -> set[str]:
    """Names of running programs that are games: installed where game stores put them
    (Steam, Epic, Rockstar, FiveM, Riot...)."""
    import psutil
    names = set()
    for p in psutil.process_iter(["name", "exe"]):
        try:
            exe = (p.info["exe"] or "").lower()
            name = (p.info["name"] or "").lower()
        except Exception:
            continue
        if name.startswith("fivem") or any(place in exe for place in _GAME_PLACES):
            names.add(name)
    return names


def _game_window(wins: list[Win]) -> Win:
    games = game_processes()
    for w in wins:                                   # top-most first: the game played last
        if w.process.lower() in games and not w.process.lower().endswith(("launcher.exe", "helper.exe")):
            return w
    raise ToolError("I can't see a game running.")


def _find_window(app: str) -> Win:
    want = app.lower().strip()
    if want in THIS:
        return active_window()
    wins = WINDOWS.list()
    if want in GAME_NAMES and not any(want in w.process.lower() for w in wins):
        return _game_window([w for w in wins if not is_nova_window(w)])
    if want in BACK_NAMES:
        others = [w for w in wins if not is_nova_window(w)]
        if len(others) < 2:
            raise ToolError("There's no other window to go back to.")
        return others[1]
    def score(w: Win) -> int:
        proc = w.process.lower().removesuffix(".exe")
        title = w.title.lower()
        return 3 if proc == want else 2 if want in proc else 1 if want in title else 0
    ranked = sorted((w for w in wins if score(w)), key=score, reverse=True)
    if not ranked:
        raise ToolError(f"I can't see a window for {app}. Is it open?")
    return ranked[0]


CLOSE_WAIT_S = 2.0
# Apps that keep running in the tray when their window is closed; no unsaved work to lose, so
# "quit" may end them outright (Spotify keeps playing otherwise).
TRAY_APPS = {"spotify": "Spotify", "discord": "Discord", "steam": "Steam", "steamwebhelper": "Steam",
             "epicgameslauncher": "Epic Games", "battle.net": "Battle.net", "eadesktop": "EA app",
             "obs64": "OBS"}


def _gone(hwnd: int) -> bool:
    import time as _t
    deadline = _t.monotonic() + CLOSE_WAIT_S
    while True:
        if all(w.hwnd != hwnd for w in WINDOWS.list()):
            return True
        if _t.monotonic() >= deadline:
            return False
        _t.sleep(0.2)


def end_processes(exe: str) -> int:
    """End every process of this program run by this user (tray apps only). How many ended."""
    import getpass

    import psutil
    me, ended = getpass.getuser().lower(), []
    for p in psutil.process_iter(["name", "username"]):
        try:
            if (p.info["name"] or "").lower() == exe.lower() and (p.info["username"] or "").lower().endswith(me):
                p.terminate()
                ended.append(p)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    psutil.wait_procs(ended, timeout=3)
    return len(ended)


def window_control(args: dict, ctx: ToolContext) -> str:
    action = args["action"]
    if action == "show_desktop":
        WINDOWS.show_desktop()
        return "Showing the desktop."
    if action == "list":
        names = sorted({(w.process.removesuffix(".exe") or w.title) for w in WINDOWS.list()})
        return "Open: " + ", ".join(names[:15]) + "." if names else "No windows are open."
    if not args.get("app"):
        raise ToolError("Which app?")
    app = args["app"].strip()
    if action in ("close", "quit", "focus") and app.lower() not in THIS + GAME_NAMES + BACK_NAMES:
        from assistant.tools import mybrowser
        if mybrowser.has_tab(app):                         # "close Gmail": the tab, not the browser
            import asyncio
            tab_action = "switch_tab" if action == "focus" else "close_tab"
            return asyncio.run(mybrowser.my_browser({"action": tab_action, "tab": app}, ctx))
    w = _find_window(app)
    exe = w.process.lower().removesuffix(".exe")
    label = TRAY_APPS.get(exe) or ("FiveM" if exe.startswith("fivem") else "") or w.process.removesuffix(".exe") or w.title
    if action == "focus":
        if not WINDOWS.focus(w.hwnd):
            raise ToolError(f"Windows wouldn't let me switch to {label}. Click on it once, then ask again.")
    elif action in ("close", "quit"):
        if action == "quit" and exe in TRAY_APPS:
            if end_processes(w.process):
                return f"Quit {label} completely."
        WINDOWS.show(w.hwnd, "close")
        # Checked: closing can be refused ("save changes?") and tray apps only hide.
        if not _gone(w.hwnd):
            return f"{label} didn't close. It may be asking you something, like whether to save."
        if exe in TRAY_APPS:
            return (f"Closed {label}'s window. It's still running in the tray; "
                    f"say \"quit {label}\" to stop it completely.")
        return f"Closed {label}."
    else:
        WINDOWS.show(w.hwnd, action)
    return {"focus": f"Switched to {label}.", "minimize": f"Minimized {label}.",
            "maximize": f"Maximized {label}.", "restore": f"Restored {label}.",
            }[action]


# Single keys only, and none that can type text, submit (Enter) or close things (Alt+F4):
# enough to control a video or a game menu, not to operate apps on the user's behalf.
KEYS = {"space": 0x20, "f": 0x46, "k": 0x4B, "m": 0x4D, "j": 0x4A, "l": 0x4C, "f11": 0x7A,
        "escape": 0x1B, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
        "page_up": 0x21, "page_down": 0x22, "home": 0x24, "end": 0x23}


async def press_key(args: dict, ctx: ToolContext) -> str:
    import asyncio

    key = args["key"].lower()
    if key not in KEYS:
        raise ToolError(f"I can only press: {', '.join(KEYS)}.")
    label = ""
    if args.get("app"):
        w = await asyncio.to_thread(_find_window, args["app"])
        await asyncio.to_thread(WINDOWS.show, w.hwnd, "focus")
        await asyncio.sleep(0.35)                 # let the window come to the front first
        label = f" in {w.process.removesuffix('.exe') or w.title}"
    await asyncio.to_thread(WINDOWS.press, KEYS[key])
    return f"Pressed {key}{label}."


# --- websites ---------------------------------------------------------------------------------
SITES = {"youtube": "https://www.youtube.com", "google": "https://www.google.com",
         "gmail": "https://mail.google.com", "reddit": "https://www.reddit.com",
         "twitch": "https://www.twitch.tv", "netflix": "https://www.netflix.com",
         "github": "https://github.com", "amazon": "https://www.amazon.com",
         "chatgpt": "https://chatgpt.com", "claude": "https://claude.ai", "maps": "https://maps.google.com"}


def _open_browser(url: str) -> None:
    from assistant.core.launch import launch
    launch(url)                     # the default browser, detached from Nova's window


def open_website(args: dict, ctx: ToolContext, _open=None) -> str:
    target = args.get("site", "").strip()
    query = args.get("search", "").strip()
    opener = _open or _open_browser
    if query:
        engines = {"youtube": "https://www.youtube.com/results?search_query=",
                   "amazon": "https://www.amazon.com/s?k=", "maps": "https://www.google.com/maps/search/"}
        base = engines.get(target.lower(), "https://www.google.com/search?q=")
        opener(base + quote_plus(query))
        return f"Searching {'Google' if base.startswith('https://www.google.com/search') else target.title()} for {query}."
    url = SITES.get(target.lower().removesuffix(".com"), target)
    if "://" not in url:
        url = "https://" + url
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or " " in parsed.netloc:
        raise ToolError("I only open normal web addresses (http or https).")
    opener(url)
    return f"Opened {parsed.netloc.removeprefix('www.')}."


def register(reg: ToolRegistry) -> None:
    reg.tool("system_status", "PC health: CPU, memory, GPU temperature/usage, battery, disk space, uptime.",
             {"type": "object", "properties": {"what": {"type": "string", "enum": [
                 "overview", "cpu", "memory", "gpu", "battery", "disk", "uptime"]}},
              "additionalProperties": False}, risk=Risk.SAFE, category="system")(system_status)
    reg.tool("lock_pc", "Lock the PC (sign-in screen).", risk=Risk.SAFE, category="system")(lock_pc)
    reg.tool("power", "Sleep, restart or shut down the PC. Always asks the user first; restart and "
             "shutdown wait 60 seconds and can be cancelled.",
             {"type": "object", "properties": {"action": {"type": "string", "enum": ["sleep", "restart", "shutdown"]}},
              "required": ["action"], "additionalProperties": False},
             risk=Risk.CONFIRM, category="system",
             describe=lambda a: {"sleep": "put the PC to sleep", "restart": "restart the PC",
                                 "shutdown": "shut down the PC"}.get(a.get("action"), "change the power state"),
             )(power)
    reg.tool("cancel_shutdown", "Cancel a pending restart or shutdown.",
             risk=Risk.SAFE, category="system")(cancel_shutdown)
    reg.tool("window", "Manage app windows: focus (switch to), minimize, maximize, restore, close "
             "(the app may ask to save), quit (also ends tray apps like Spotify/Discord), list open apps, "
             "or show_desktop. app='this' = the window in use.",
             {"type": "object", "properties": {
                 "action": {"type": "string", "enum": ["focus", "minimize", "maximize", "restore",
                                                       "close", "quit", "list", "show_desktop"]},
                 "app": {"type": "string", "maxLength": 80}},
              "required": ["action"], "additionalProperties": False},
             risk=Risk.SAFE, category="apps")(window_control)
    reg.tool("press_key", "Press one key, optionally in an app first brought to the front. For videos "
             "(YouTube, a browser, a player): 'f' = fullscreen the video, 'space' or 'k' = pause/play, "
             "'m' = mute, 'j'/'l' = back/forward 10 s, 'escape' = leave fullscreen, 'f11' = browser "
             "fullscreen. Example: fullscreen a YouTube video -> key 'f', app 'chrome'.",
             {"type": "object", "properties": {"key": {"type": "string", "enum": list(KEYS)},
                                               "app": {"type": "string", "maxLength": 80}},
              "required": ["key"], "additionalProperties": False}, risk=Risk.SAFE, category="apps")(press_key)
    reg.tool("open_website", "Open a website in the browser (site = name like 'youtube' or an address), "
             "or search (search = words; site = 'youtube'/'amazon'/'maps' to search there, else Google).",
             {"type": "object", "properties": {"site": {"type": "string", "maxLength": 200},
                                               "search": {"type": "string", "maxLength": 200}},
              "additionalProperties": False}, risk=Risk.SAFE, category="web")(open_website)
