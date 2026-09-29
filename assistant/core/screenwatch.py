"""Nova notices trouble on the screen by himself. Owner's pick for the next feature: "seamless PC
control and real-time screen interaction, like reading errors, managing apps, or helping with tasks
without needing a mouse".

Every few seconds (cheap: a list of windows, nothing sent anywhere):
- a new error box (a Windows dialog whose title or text reads like a problem) -> read it with the
  screen reader (tools/ocr.py) and say so once: "Sir, Steam says: Disk write error. Say 'fix it' and
  I'll sort it out.";
- an app that stops responding for a few checks in a row -> "Sir, Discord isn't responding."
Then "fix it", "what does it say", "close it" and "ignore it" are about that alert (the brain asks
`recent()`). While a full-screen game is in front, alerts are kept quiet (still there if asked).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

PROBLEM = re.compile(
    r"\b(?:error|failed|failure|fatal|cannot|can'?t|couldn'?t|could not|unable|not responding|stopped working|"
    r"crash(?:ed)?|exception|denied|missing|not found|invalid|corrupt(?:ed)?|problem|warning|unexpected)\b", re.I)
RECENT_S = 180              # "fix it" means the last alert for three minutes
HUNG_CHECKS = 3             # checks in a row before an app counts as frozen (loading screens hiccup)


@dataclass
class Alert:
    kind: str                # "error" | "frozen"
    hwnd: int
    app: str                 # the program, e.g. "Steam"
    title: str
    text: str = ""
    at: float = 0.0
    said: bool = False

    def spoken(self, sir: str = "sir") -> str:
        who = f"{sir.capitalize()}, " if sir else ""
        if self.kind == "frozen":
            return f"{who}{self.app} isn't responding. Say 'close it' to end it, or 'ignore it'."
        what = (self.text or self.title).strip().rstrip(".!")
        what = what if len(what) <= 140 else what[:137].rsplit(" ", 1)[0] + "..."
        return f"{who}{self.app} says: {what}. Say 'fix it' and I'll sort it out."

    def describe(self) -> str:
        """For Claude or the model: what's on screen."""
        if self.kind == "frozen":
            return f"{self.app} ('{self.title}') is not responding."
        return f"An error box from {self.app}, titled '{self.title}', says: {self.text or '(no text read)'}"


def _app_name(process: str, title: str) -> str:
    exe = process.lower().removesuffix(".exe")
    names = {"steam": "Steam", "steamwebhelper": "Steam", "discord": "Discord", "spotify": "Spotify",
             "firefox": "Firefox", "msedge": "Edge", "chrome": "Chrome", "explorer": "File Explorer",
             "notepad": "Notepad", "winword": "Word", "excel": "Excel", "obs64": "OBS"}
    return names.get(exe) or (exe.capitalize() if exe else (title or "An app"))


class ScreenWatch:
    def __init__(self, notify, interval_s: float = 3.0, read_text=None, clock=time.monotonic):
        self.notify = notify                         # async text -> None (VoiceLoop.announce)
        self.interval_s = interval_s
        self.read_text = read_text or _read_dialog
        self.clock = clock
        self.last: Alert | None = None
        self._seen: set[tuple[int, str]] = set()
        self._hung: dict[int, int] = {}
        self._first = True
        self.on = True

    def recent(self) -> Alert | None:
        a = self.last
        return a if a is not None and self.clock() - a.at < RECENT_S else None

    def dismiss(self) -> None:
        self.last = None

    def scan(self) -> list[Alert]:
        """One look (in a worker thread). New alerts only."""
        from assistant.tools import pc
        found: list[Alert] = []
        dialogs = getattr(pc.WINDOWS, "dialogs", None)
        for d in (dialogs() if dialogs else []):
            key = (d.hwnd, d.title)
            if key in self._seen:
                continue
            self._seen.add(key)
            if self._first:
                continue                             # whatever was already open when Nova started
            text = ""
            try:
                text = self.read_text(d.hwnd)
            except Exception as e:
                log.debug("couldn't read a dialog: %s", e)
            if PROBLEM.search(f"{d.title} {text}"):
                found.append(Alert("error", d.hwnd, _app_name(d.process, d.title), d.title, text, self.clock()))
        hung = getattr(pc.WINDOWS, "hung", None)
        if hung is not None:
            alive = set()
            for w in pc.WINDOWS.list():
                alive.add(w.hwnd)
                try:
                    frozen = hung(w.hwnd)
                except Exception:
                    frozen = False
                n = self._hung.get(w.hwnd, 0) + 1 if frozen else 0
                self._hung[w.hwnd] = n
                if n == HUNG_CHECKS:
                    found.append(Alert("frozen", w.hwnd, _app_name(w.process, w.title), w.title, at=self.clock()))
            self._hung = {h: n for h, n in self._hung.items() if h in alive}
        self._first = False
        return found

    async def check(self) -> None:
        alerts = await asyncio.to_thread(self.scan)
        for a in alerts:
            self.last = a
            if await asyncio.to_thread(_game_in_front):
                log.info("screen alert held during a game: %s", a.describe())
                continue
            a.said = True
            await self.notify(a.spoken(""))

    async def run(self) -> None:
        while True:
            if self.on:
                try:
                    await self.check()
                except Exception as e:               # never let the watcher die
                    log.debug("screen watch: %s", e)
            await asyncio.sleep(self.interval_s)


def _read_dialog(hwnd: int) -> str:
    """The words in a dialog, read off the screen."""
    from assistant.tools import ocr, pc
    area = pc.WINDOWS.rect(hwnd)
    bgra, w, h, left, top = ocr.OCR.capture(area)
    lines = ocr.OCR.read(bgra, w, h)
    skip = {"ok", "cancel", "yes", "no", "retry", "close", "help", "ignore", "abort", "continue"}
    words = [ln.text for ln in lines if ln.text.strip().lower() not in skip]
    return " ".join(words)[:400]


def _game_in_front() -> bool:
    from assistant.tools import pc
    try:
        w = pc.WINDOWS.active()
        return bool(w and pc.WINDOWS.is_fullscreen(w.hwnd))
    except Exception:
        return False
