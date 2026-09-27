"""'Tell me when ...': Nova keeps an eye on something and speaks up when it happens.

Each watch checks one thing every few seconds (a download folder, an app, a temperature,
the battery, the internet, a web page) and announces itself once, like a reminder: with a
chime, never over Nova's own speech, held while standing down. Watches last this session
(up to 24 hours); at most 10 at a time.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import os
import re
import socket
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

PARTIAL = (".crdownload", ".part", ".partial", ".download", ".opdownload", ".!ut", ".tmp")
MAX_WATCHES = 10
MAX_AGE_S = 24 * 3600
KINDS = ["download", "steam_download", "app_closes", "app_done", "gpu_below", "gpu_above",
         "cpu_below", "battery_above", "battery_below", "internet_back", "page_changes"]


# --- probes: how each thing is measured (swappable in tests) -----------------------------------
class Probes:
    def download_dirs(self) -> list[Path]:
        home = Path.home()
        return [d for d in (home / "Downloads", home / "OneDrive" / "Downloads") if d.exists()]

    def partial_files(self) -> set[str]:
        out = set()
        for d in self.download_dirs():
            try:
                for e in os.scandir(d):
                    if e.is_file() and e.name.lower().endswith(PARTIAL):
                        out.add(e.path)
            except OSError:
                pass
        return out

    def steam_downloading(self) -> set[str]:
        """Steam puts games that are downloading in steamapps/downloading/<appid>."""
        out = set()
        for lib in self.steam_libraries():
            d = lib / "steamapps" / "downloading"
            try:
                out |= {e.name for e in os.scandir(d) if e.is_dir() and any(os.scandir(e.path))}
            except OSError:
                pass
        return out

    def steam_libraries(self) -> list[Path]:
        roots = []
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
                roots.append(Path(winreg.QueryValueEx(k, "SteamPath")[0]))
        except Exception:
            roots.append(Path("C:/Program Files (x86)/Steam"))
        libs = list(roots)
        for root in roots:
            vdf = root / "steamapps" / "libraryfolders.vdf"
            try:
                libs += [Path(p.replace("\\\\", "\\")) for p in re.findall(r'"path"\s+"([^"]+)"', vdf.read_text("utf-8"))]
            except OSError:
                pass
        return [p for p in dict.fromkeys(libs) if p.exists()]

    def processes(self, name: str) -> list[Any]:
        import psutil
        want = name.lower().removesuffix(".exe")
        out = []
        for p in psutil.process_iter(["name"]):
            n = (p.info.get("name") or "").lower().removesuffix(".exe")
            if n == want or (len(want) >= 4 and want in n):
                out.append(p)
        return out

    def cpu_of(self, procs: list[Any]) -> float:
        total = 0.0
        for p in procs:
            try:
                total += p.cpu_percent(None)
            except Exception:
                pass
        import psutil
        return total / max(1, psutil.cpu_count() or 1)

    def gpu_temp(self) -> float | None:
        from assistant.tools.pc import gpu_stats
        g = gpu_stats()
        return float(g["temp"]) if g else None

    def cpu_percent(self) -> float:
        import psutil
        return psutil.cpu_percent(None)

    def battery(self) -> tuple[float, bool] | None:
        import psutil
        b = psutil.sensors_battery()
        return (b.percent, bool(b.power_plugged)) if b else None

    def online(self) -> bool:
        for host in ("1.1.1.1", "8.8.8.8"):
            try:
                with socket.create_connection((host, 443), timeout=2):
                    return True
            except OSError:
                continue
        return False

    def page_fingerprint(self, url: str) -> str:
        import httpx
        r = httpx.get(url, timeout=15, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0 Nova"})
        text = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", r.text)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\d+", "#", " ".join(text.split()))       # ignore clocks and counters
        return hashlib.sha256(text.encode()).hexdigest()


# --- a watch -------------------------------------------------------------------------------------
@dataclass
class Watch:
    kind: str
    target: str = ""                  # app name, URL, or file words
    value: float | None = None        # temperature / percent threshold
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:6])
    created: float = field(default_factory=time.time)
    state: dict = field(default_factory=dict)

    def describe(self) -> str:
        t, v = self.target, self.value
        return {
            "download": f"your download{f' ({t})' if t else ''} finishing",
            "steam_download": "your Steam download finishing",
            "app_closes": f"{t} closing",
            "app_done": f"{t} finishing its work",
            "gpu_below": f"the GPU cooling below {v:.0f}°C" if v else "the GPU cooling down",
            "gpu_above": f"the GPU going above {v:.0f}°C" if v else "the GPU getting hot",
            "cpu_below": f"the CPU going below {v:.0f}% busy" if v else "the CPU calming down",
            "battery_above": "the battery being full" if not v or v >= 99 else f"the battery reaching {v:.0f}%",
            "battery_below": f"the battery dropping below {v:.0f}%" if v else "the battery getting low",
            "internet_back": "the internet coming back",
            "page_changes": f"{_site(t)} changing",
        }.get(self.kind, self.kind)

    def interval(self) -> float:
        return {"page_changes": 60.0, "internet_back": 5.0, "download": 3.0}.get(self.kind, 5.0)


def _site(url: str) -> str:
    m = re.match(r"https?://(?:www\.)?([^/]+)", url or "")
    return m.group(1) if m else "the page"


class WatchError(ValueError):
    pass


def check(w: Watch, p: Probes) -> str | None:
    """None while waiting; the sentence to say when it has happened."""
    s = w.state
    if w.kind == "download":
        now = {f for f in p.partial_files() if not w.target or w.target.lower() in Path(f).name.lower()}
        if "seen" not in s:
            s["seen"], s["t0"] = set(now), time.time()
        s["seen"] |= now
        if s["seen"] and not now:
            names = sorted(_final_name(f) for f in s["seen"])
            what = names[0] if len(names) == 1 else f"{len(names)} downloads"
            return f"Your download is finished: {what}."
        if not s["seen"] and time.time() - s["t0"] > 600:
            return "I didn't see a download start in the last ten minutes, so I've stopped watching."
        return None
    if w.kind == "steam_download":
        now = p.steam_downloading()
        if "seen" not in s:
            s["seen"], s["t0"] = set(now), time.time()
        s["seen"] |= now
        if s["seen"] and not now:
            return "Steam has finished downloading."
        if not s["seen"] and time.time() - s["t0"] > 600:
            return "Steam isn't downloading anything, so I've stopped watching."
        return None
    if w.kind in ("app_closes", "app_done"):
        procs = p.processes(w.target)
        if w.kind == "app_closes" or not procs:
            if not procs and s.get("was_running", True):
                return f"{w.target.capitalize()} has closed."
            s["was_running"] = True
            return None
        cpu = p.cpu_of(procs)
        s["busy"] = s.get("busy", False) or cpu > 8
        quiet = s.get("quiet", 0) + 1 if cpu < 2 else 0
        s["quiet"] = quiet
        if s["busy"] and quiet >= 3:                 # worked, then ~15 s of calm
            return f"{w.target.capitalize()} looks finished: it's gone quiet."
        return None
    if w.kind in ("gpu_below", "gpu_above"):
        t = p.gpu_temp()
        if t is None:
            return "I can't read the GPU temperature, so I've stopped watching it."
        if w.kind == "gpu_below" and t <= (w.value or 60):
            return f"Your GPU has cooled down to {t:.0f}°C."
        if w.kind == "gpu_above" and t >= (w.value or 85):
            return f"Heads up: your GPU is at {t:.0f}°C."
        return None
    if w.kind == "cpu_below":
        c = p.cpu_percent()
        return f"The CPU has calmed down: {c:.0f}% busy." if c <= (w.value or 20) else None
    if w.kind in ("battery_above", "battery_below"):
        b = p.battery()
        if b is None:
            return "This PC has no battery reading, so I've stopped watching it."
        pct, plugged = b
        if w.kind == "battery_above" and pct >= (w.value or 100) - (1 if not w.value or w.value >= 99 else 0):
            return "The battery is full." if (w.value or 100) >= 99 else f"The battery is at {pct:.0f}%."
        if w.kind == "battery_below" and pct <= (w.value or 20) and not plugged:
            return f"The battery is down to {pct:.0f}%. Time to plug in."
        return None
    if w.kind == "internet_back":
        return "The internet is back." if p.online() else None
    if w.kind == "page_changes":
        fp = p.page_fingerprint(w.target)
        if "fp" not in s:
            s["fp"] = fp
            return None
        return f"{_site(w.target)} has changed." if fp != s["fp"] else None
    raise WatchError(f"Unknown kind of watch: {w.kind}")


def _final_name(partial: str) -> str:
    name = Path(partial).name
    for ext in PARTIAL:
        if name.lower().endswith(ext):
            name = name[: -len(ext)]
    return name or "a file"


# --- the manager -----------------------------------------------------------------------------------
class Watchers:
    def __init__(self, notify: Callable[[str], Any] | None = None, probes: Probes | None = None,
                 clock: Callable[[], float] = time.time):
        self.notify, self.p, self.clock = notify, probes or Probes(), clock
        self.items: dict[str, Watch] = {}
        self._next: dict[str, float] = {}
        self._wake = asyncio.Event()
        self.listeners: list = []

    def add(self, w: Watch) -> Watch:
        if w.kind not in KINDS:
            raise WatchError(f"I can't watch for that ({w.kind}).")
        if len(self.items) >= MAX_WATCHES:
            raise WatchError(f"I'm already watching {MAX_WATCHES} things. Cancel one first.")
        if w.kind in ("app_closes", "app_done") and not self.p.processes(w.target):
            raise WatchError(f"{w.target} isn't running.")
        if w.kind == "page_changes" and not re.match(r"https?://", w.target or ""):
            raise WatchError("Which web page? I need its address.")
        self.items[w.id] = w
        self._next[w.id] = 0.0
        self._wake.set()
        self._changed()
        return w

    def cancel(self, which: str = "") -> list[Watch]:
        w = which.strip().lower()
        items = list(self.items.values())
        if w in ("all", "everything", "all of them"):
            hits = items
        elif w:
            hits = [x for x in items if x.id == w or w in x.describe().lower() or w in x.kind]
        else:
            hits = items[-1:]
        for x in hits:
            self.items.pop(x.id, None)
            self._next.pop(x.id, None)
        if hits:
            self._changed()
        return hits

    def _changed(self) -> None:
        for fn in list(self.listeners):
            try:
                fn()
            except Exception:
                pass

    async def tick(self) -> None:
        """Check whatever is due (in a worker thread: probes can block briefly)."""
        now = self.clock()
        for w in list(self.items.values()):
            if self._next.get(w.id, 0) > now:
                continue
            self._next[w.id] = now + w.interval()
            if now - w.created > MAX_AGE_S:
                msg = f"I've stopped watching for {w.describe()}: it's been a day."
            else:
                try:
                    msg = await asyncio.to_thread(check, w, self.p)
                except Exception as e:
                    log.warning("watch %s failed: %s", w.kind, e)
                    w.state["errors"] = w.state.get("errors", 0) + 1
                    msg = (f"I couldn't keep watching for {w.describe()}." if w.state["errors"] >= 5 else None)
            if msg:
                self.items.pop(w.id, None)
                self._next.pop(w.id, None)
                self._changed()
                out = self.notify(msg) if self.notify else None
                if inspect.isawaitable(out):
                    await out

    async def run(self) -> None:
        while True:
            await self.tick()
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), 1.0)
            except asyncio.TimeoutError:
                pass
