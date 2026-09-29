"""Scheduler, timer tools, and voice announcements."""

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from assistant.core.scheduler import Scheduler, human_duration, parse_clock
from assistant.tools import timers
from assistant.tools.registry import ToolContext, ToolError

TZ = "America/Chicago"


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def sched(tmp_path):
    return Scheduler(tmp_path / "reminders.json", TZ, clock=Clock())


def test_parse_clock():
    now = datetime(2026, 9, 27, 15, 10, tzinfo=ZoneInfo(TZ))            # 3:10 pm
    assert parse_clock("7:30 pm", TZ, now).strftime("%d %H:%M") == "27 19:30"
    assert parse_clock("19:30", TZ, now).strftime("%d %H:%M") == "27 19:30"
    assert parse_clock("7 am", TZ, now).strftime("%d %H:%M") == "28 07:00"   # tomorrow
    assert parse_clock("7", TZ, now).strftime("%d %H:%M") == "27 19:00"      # afternoon: 7 pm
    assert parse_clock("noon", TZ, now).strftime("%d %H:%M") == "28 12:00"
    assert parse_clock("12 am", TZ, now).strftime("%H:%M") == "00:00"
    with pytest.raises(ValueError):
        parse_clock("banana", TZ, now)


def test_human_duration():
    assert human_duration(1200) == "20 minutes"
    assert human_duration(3660) == "1 hour and 1 minute"
    assert human_duration(90) == "1 minute and 30 seconds"


def test_persistence_and_cancel(tmp_path, sched):
    sched.add("timer", sched.clock() + 60, "pasta")
    sched.add("reminder", sched.clock() + 600, "call mum")
    sched.add("alarm", sched.clock() + 9000, "")
    again = Scheduler(tmp_path / "reminders.json", TZ, clock=sched.clock)
    assert [r.kind for r in again.upcoming()] == ["timer", "reminder", "alarm"]
    assert [r.text for r in again.cancel("mum")] == ["call mum"]
    assert [r.kind for r in again.cancel("alarm")] == ["alarm"]
    assert len(again.cancel("all")) == 1 and again.upcoming() == []


async def test_due_items_fire_and_missed_ones_are_marked(tmp_path):
    clock = Clock()
    fired = []
    s = Scheduler(tmp_path / "r.json", TZ, notify=lambda r, missed: fired.append((r.text, missed)), clock=clock)
    s.add("reminder", clock.t - 3600, "old one")          # came due while Nova was off
    s.add("timer", clock.t + 0.1, "pasta")
    task = asyncio.create_task(s.run())
    await asyncio.sleep(0.05)
    clock.t += 1
    s._wake.set()
    await asyncio.sleep(0.05)
    task.cancel()
    assert fired == [("old one", True), ("pasta", False)]
    assert s.upcoming() == []


def test_spoken_text():
    from assistant.core.scheduler import Reminder
    assert Reminder("a", "timer", 0, "pasta", 0).spoken(TZ) == "Your pasta timer is done."
    assert Reminder("b", "reminder", 0, "call mum", 0).spoken(TZ) == "A reminder: call mum."
    assert "while I was off" in Reminder("c", "reminder", 0, "x", 0).spoken(TZ, missed=True)


def test_timer_tools(settings, sched):
    ctx = ToolContext(settings, services={"scheduler": sched})
    assert timers.set_timer({"minutes": 20, "label": "pasta"}, ctx) == "Timer set for the pasta: 20 minutes."
    out = timers.set_reminder({"text": "call mum", "minutes": 60}, ctx)
    assert out == "I'll remind you in 1 hour: call mum."
    assert timers.set_alarm({"at": "7 am"}, ctx).startswith("Alarm set for 7:00 AM")
    listing = timers.list_timers({}, ctx)
    assert "timer (pasta): 20 minutes left" in listing and "reminder (call mum)" in listing
    assert timers.cancel_timer({"which": "pasta"}, ctx) == "Cancelled the timer for pasta."
    with pytest.raises(ToolError):
        timers.set_timer({}, ctx)
    with pytest.raises(ToolError, match="available in this mode"):
        timers.set_timer({"minutes": 1}, ToolContext(settings))


async def test_voice_announcement_waits_and_chimes(settings, registry):
    from tests.fakes import text_msg
    from tests.test_barge import make
    loop, events = make(settings, registry, [text_msg("Certainly, sir. It is noon.")], [
        ("say", "Nova, what time is it", 10), ("quiet", 60)])
    played = []
    orig = loop.player.play
    loop.player.play = lambda a: (played.append(len(a)), orig(a))

    async def fire_during_reply():
        while not loop.busy:
            await asyncio.sleep(0.001)
        await loop.announce("Your pasta timer is done.")
    t = asyncio.create_task(fire_during_reply())
    await loop.run()
    await t
    for _ in range(100):
        if "Sir, your pasta timer is done." in loop.tts.spoken:
            break
        await asyncio.sleep(0.01)
    spoken = loop.tts.spoken
    assert spoken.index("Sir, your pasta timer is done.") > spoken.index("It is noon.")   # waited
    assert any(e["type"] == "announcement" for e in events)


async def test_announcement_held_during_standby(settings, registry):
    from tests.test_barge import make
    loop, events = make(settings, registry, [], [])
    loop.standby = True
    await loop.announce("A reminder: call mum.")
    assert loop._announcements == ["A reminder: call mum."]
    assert loop.tts.spoken == []


def test_chime():
    from assistant.voice.pipeline import chime
    c = chime()
    assert c.dtype == np.float32 and 0.2 < len(c) / 24000 < 0.6 and np.max(np.abs(c)) <= 0.3


async def test_an_announcement_can_be_talked_over(settings, registry):
    """Owner: "he isn't letting me interrupt": a job's "All done, sir..." played to the end, because
    Nova didn't count as speaking during announcements. Now he does, so barge-in and stop work."""
    from tests.test_barge import make
    loop, events = make(settings, registry, [], [])
    seen = []
    orig = loop.say

    async def say(text, show=True):
        await orig(text, show)
        seen.append((loop.busy, loop.speaking))       # as it was while the words played
    loop.say = say
    await loop.announce("All done. The answers are saved.")
    assert seen == [(True, True)] and not loop.busy and not loop._announcing
