"""Owner: 'when I tell him to cancel he doesn't cancel things, and he pauses and freezes'.
Universal cancel, time limits, 'still working' lines, the 'heard you' tick, media, corrections."""

import asyncio
import time

import numpy as np
import pytest

from assistant.brain.intents import match_intent
from assistant.core.conversation import Conversation
from assistant.core.scheduler import Scheduler
from assistant.tools import registry as R
from assistant.tools import video as V
from assistant.tools.registry import Risk, ToolContext, ToolError
from assistant.voice.commands import is_cancel
from assistant.voice.pipeline import tick
from tests.fakes import text_msg, tool_msg
from tests.test_barge import STORY, make
from tests.voice_helpers import FakeTTS


@pytest.mark.parametrize("text", ["Cancel.", "cancel that", "Nova, cancel", "never mind", "nevermind",
                                  "forget it", "no wait, cancel that", "undo that", "Cancel it please"])
def test_cancel_phrases(text):
    assert is_cancel(text)


@pytest.mark.parametrize("text", ["cancel the timer", "cancel my 5pm reminder", "don't cancel it",
                                  "I want to cancel my subscription", "stop the music"])
def test_not_a_bare_cancel(text):
    assert not is_cancel(text)


def with_scheduler(loop, tmp_path, settings):
    sched = Scheduler(tmp_path / "rem.json", settings.assistant.timezone)
    loop.ctx.services["scheduler"] = sched
    return sched


async def test_cancel_undoes_the_timer_just_set(settings, registry, tmp_path):
    loop, events = make(settings, registry, [tool_msg("set_timer", {"minutes": 5, "label": "pasta"}),
                                             text_msg("Timer set, sir.")], [
        ("say", "Nova, set a timer for five minutes for the pasta", 10), ("quiet", 20),
        ("until", lambda l: l.turns_done >= 1 and not l.busy),
        ("say", "cancel that", 10), ("quiet", 20),                 # no name: right after Nova's reply
        ("until", lambda l: l.turns_done >= 2 and not l.busy)], tts=FakeTTS())
    sched = with_scheduler(loop, tmp_path, settings)
    await loop.run()
    assert sched.upcoming() == []
    assert "Cancelled the timer for pasta." in loop.tts.spoken


async def test_cancel_stops_a_restart_countdown(settings, registry):
    from assistant.tools import pc
    calls = []
    pc.POWER.shutdown, old = (lambda a: calls.append(a)), pc.POWER.shutdown
    try:
        loop, _ = make(settings, registry, [], [("quiet", 1)], tts=FakeTTS())

        async def yes(*a):
            return True
        loop.ctx.confirm = yes
        await registry.execute("power", {"action": "restart"}, loop.ctx)
        loop._follow_up_until = time.perf_counter() + 5
        assert await loop._control_words("never mind")
        assert calls[-1] == ["/a"] and "Cancelled the restart." in loop.tts.spoken
    finally:
        pc.POWER.shutdown = old


async def test_cancel_in_game_chat_is_ignored(settings, registry, tmp_path):
    loop, events = make(settings, registry, [], [("quiet", 1)], tts=FakeTTS())
    sched = with_scheduler(loop, tmp_path, settings)
    sched.add("timer", time.time() + 300, "tea")
    loop._note_action("set_timer", {"minutes": 5}, True)
    assert not await loop._control_words("cancel")                   # not addressed, no follow-up
    assert len(sched.upcoming()) == 1
    assert await loop._control_words("Nova, cancel")
    assert sched.upcoming() == []


async def test_cancel_while_talking_and_when_nothing_to_cancel(settings, registry):
    loop, events = make(settings, registry, [STORY], [
        ("say", "Nova, tell me a story", 10), ("quiet", 20),
        ("until", lambda l: l.speaking),
        ("say", "cancel that", 10), ("quiet", 20)])
    await loop.run()
    assert "The end." not in loop.tts.spoken and "Cancelled, sir." in loop.tts.spoken
    loop2, _ = make(settings, registry, [], [("quiet", 1)], tts=FakeTTS())
    assert await loop2._control_words("Nova, cancel")
    assert loop2.tts.spoken[-1] == "There's nothing to cancel, sir."


async def test_cancel_hides_the_grid(settings, registry):
    from assistant.tools import grid as G
    from tests.test_grid import FakeMouse, FakeOverlay
    loop, _ = make(settings, registry, [], [("quiet", 1)], tts=FakeTTS())
    g = loop.ctx.services["grid"] = G.GridController(mouse=FakeMouse(), overlay=FakeOverlay())
    g.show()
    assert await loop._control_words("cancel")                        # grid showing: no name needed
    assert not g.visible


async def test_a_stuck_tool_times_out(settings, registry, monkeypatch):
    @registry.tool("stuck", "never finishes", risk=Risk.SAFE)
    async def stuck(args, ctx):
        await asyncio.sleep(60)
    monkeypatch.setitem(R.TOOL_TIMEOUTS, "stuck", 0.05)
    res = await registry.execute("stuck", {}, ToolContext(settings))
    assert res.is_error and "took too long" in res.content
    assert registry.audit.tail()[-1]["status"] == "timeout"


async def test_slow_reply_says_one_moment(settings, registry):
    settings.voice.still_working_s = 0.05
    loop, _ = make(settings, registry, [text_msg("It is noon, sir.")], [
        ("say", "Nova, what time is it", 10), ("quiet", 20),
        ("until", lambda l: l.turns_done >= 1 and not l.busy)], tts=FakeTTS(), delay=0.3)
    await loop.run()
    assert loop.tts.spoken[0] in settings.voice.filler_phrases and loop.tts.spoken[-1] == "It is noon, sir."


async def test_quick_reply_has_no_filler_but_a_tick(settings, registry):
    loop, _ = make(settings, registry, [text_msg("It is noon, sir.")], [
        ("say", "Nova, what time is it", 10), ("quiet", 20),
        ("until", lambda l: l.turns_done >= 1 and not l.busy)], tts=FakeTTS())
    await loop.run()
    assert loop.tts.spoken == ["It is noon, sir."]
    assert any(c.size == tick().size and np.allclose(c, tick()) for c in loop.player.chunks)


class FakeMedia:
    def __init__(self, items):
        self.items, self.cmds = items, []

    async def list(self):
        return self.items

    async def command(self, m, action):
        self.cmds.append((m.title, action))
        if action in ("play", "pause"):
            m.status = "playing" if action == "play" else "paused"      # a player that obeys
        return True


async def test_pause_means_whatever_is_playing(settings):
    video = V.Media(0, "firefox.exe", "Lofi beats", "", "playing")
    spotify = V.Media(1, "Spotify.exe", "My Way", "", "paused")
    api = FakeMedia([spotify, video])
    assert await V.media({"action": "pause"}, ToolContext(settings), _media=api) == "Paused Lofi beats."
    assert await V.media({"action": "pause"}, ToolContext(settings), _media=api) == "Nothing is playing."
    api2 = FakeMedia([V.Media(0, "Spotify.exe", "My Way", "", "playing")])
    assert (await V.media({"action": "play"}, ToolContext(settings), _media=api2)).endswith("already playing.")


def test_plain_pause_goes_to_whatever_plays():
    assert match_intent("Pause") == ("media", {"action": "pause"})
    assert match_intent("resume") == ("media", {"action": "play"})


async def test_corrections_are_the_request(settings, ctx):
    from tests.test_local_brain import collect
    from tests.test_local_brain import make as make_brain
    settings.brain.backend = "local"
    brain, fake = make_brain(settings, [])
    ran = []

    async def fake_media(args, c):
        ran.append(args)
        return "Paused."
    brain.registry.get("media").handler = fake_media
    await collect(brain, Conversation(), "No, I meant pause", ctx)
    assert ran == [{"action": "pause"}] and fake.requests == []



async def test_pause_that_doesnt_happen_is_reported(settings, monkeypatch):
    """A player that ignores Windows: the media key is tried, then Nova says it didn't work."""
    from assistant.tools import music
    pressed = []
    monkeypatch.setattr(music, "press_media_key", pressed.append)

    class Stubborn(FakeMedia):
        async def command(self, m, action):
            return True                                            # "accepted", nothing happens
    video = V.Media(0, "firefox.exe", "Lofi beats", "", "playing")
    with pytest.raises(ToolError, match="didn't pause"):
        await V.media({"action": "pause"}, ToolContext(settings), _media=Stubborn([video]))
    assert pressed == ["play_pause"]
