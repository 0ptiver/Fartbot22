"""Timers, reminders and alarms: persisted to data/reminders.json, announced when due."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)


@dataclass
class Reminder:
    id: str
    kind: str          # timer | reminder | alarm
    due: float         # epoch seconds
    text: str          # label / what to remind about
    created: float

    def spoken(self, tz: str, missed: bool = False) -> str:
        what = self.text.strip()
        if self.kind == "timer":
            base = f"your {what} timer is done" if what else "your timer is done"
        elif self.kind == "alarm":
            base = f"it's {_clock(self.due, tz)}" + (f": {what}" if what else ", your alarm")
        else:
            base = f"a reminder: {what}" if what else "a reminder"
        if missed:
            base += f" (that was due at {_clock(self.due, tz)} while I was off)"
        return base[0].upper() + base[1:] + "."


def _clock(epoch: float, tz: str) -> str:
    return datetime.fromtimestamp(epoch, ZoneInfo(tz)).strftime("%I:%M %p").lstrip("0")


class Scheduler:
    def __init__(self, path: Path, tz: str, notify: Callable[[Reminder, bool], Any] | None = None,
                 clock: Callable[[], float] = time.time):
        self.path, self.tz, self.notify, self.clock = path, tz, notify, clock
        self._items: dict[str, Reminder] = {}
        self._wake = asyncio.Event()
        self._load()

    # --- persistence -----------------------------------------------------------------
    def _load(self) -> None:
        try:
            for raw in json.loads(self.path.read_text(encoding="utf-8")):
                r = Reminder(**raw)
                self._items[r.id] = r
        except FileNotFoundError:
            pass
        except Exception as e:
            log.warning("couldn't read %s: %s", self.path, e)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps([asdict(r) for r in self._items.values()], indent=1), encoding="utf-8")
        tmp.replace(self.path)

    # --- API ---------------------------------------------------------------------------
    def add(self, kind: str, due: float, text: str = "") -> Reminder:
        r = Reminder(uuid.uuid4().hex[:8], kind, due, text.strip(), self.clock())
        self._items[r.id] = r
        self._save()
        self._wake.set()
        return r

    def upcoming(self) -> list[Reminder]:
        return sorted(self._items.values(), key=lambda r: r.due)

    def cancel(self, which: str = "") -> list[Reminder]:
        """Cancel by id, by words in the label, by kind ('timer', 'alarm'), or 'all'.
        With no match criteria: the next one due."""
        w = which.strip().lower()
        items = self.upcoming()
        if w in ("all", "everything", "all of them"):
            hits = items
        elif w in ("timer", "timers", "reminder", "reminders", "alarm", "alarms"):
            hits = [r for r in items if r.kind == w.rstrip("s")]
        elif w:
            hits = [r for r in items if r.id == w or all(x in r.text.lower() for x in w.split())]
        else:
            hits = items[:1]
        for r in hits:
            self._items.pop(r.id, None)
        if hits:
            self._save()
        return hits

    # --- running -----------------------------------------------------------------------
    async def run(self) -> None:
        """Fire due items forever. Items that came due while Nova was off fire once, marked."""
        started = self.clock()
        while True:
            now = self.clock()
            due = [r for r in self.upcoming() if r.due <= now]
            for r in due:
                self._items.pop(r.id, None)
            if due:
                self._save()
            for r in due:
                missed = r.due < started - 60
                try:
                    out = self.notify(r, missed) if self.notify else None
                    if inspect.isawaitable(out):
                        await out
                except Exception:
                    log.exception("reminder notification failed")
            nxt = self.upcoming()
            delay = min(max(nxt[0].due - self.clock(), 0.05), 30.0) if nxt else 30.0
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), delay)
            except asyncio.TimeoutError:
                pass


# --- time parsing -----------------------------------------------------------------------------
_CLOCK = re.compile(r"^\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?\s*$", re.I)


def parse_clock(text: str, tz: str, now: datetime | None = None) -> datetime:
    """'7:30 pm', '19:30', '7am', 'noon', 'midnight' -> next such time (today or tomorrow)."""
    t = text.strip().lower().replace("o'clock", "").strip()
    now = now or datetime.now(ZoneInfo(tz))
    if t in ("noon", "midday"):
        h, m = 12, 0
    elif t == "midnight":
        h, m = 0, 0
    else:
        m_ = _CLOCK.match(t)
        if not m_:
            raise ValueError(f"I didn't understand the time '{text}'. Try something like 7:30 pm.")
        h, m = int(m_.group(1)), int(m_.group(2) or 0)
        ampm = (m_.group(3) or "").replace(".", "")
        if ampm == "pm" and h < 12:
            h += 12
        elif ampm == "am" and h == 12:
            h = 0
        if not ampm and h <= 12 and h < now.hour and h + 12 > now.hour:
            h += 12          # "at 7" in the afternoon means 7 pm, not tomorrow morning
        if h > 23 or m > 59:
            raise ValueError(f"'{text}' isn't a valid time.")
    target = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


def human_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    parts = [f"{h} hour{'s' if h != 1 else ''}"] if h else []
    if m:
        parts.append(f"{m} minute{'s' if m != 1 else ''}")
    if s and not h:
        parts.append(f"{s} second{'s' if s != 1 else ''}")
    return " and ".join(parts) or "a moment"
