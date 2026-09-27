"""Spoken confirmations and the 'stand down' kill switch."""

import pytest


from assistant.tools.registry import Risk
from assistant.voice.commands import classify_yes_no, is_resume, is_stand_down
from tests.fakes import text_msg, tool_msg
from tests.test_barge import STORY, make
from tests.voice_helpers import FakeTTS


@pytest.mark.parametrize("text,answer", [
    ("Yes.", True), ("Yeah, go ahead", True), ("Do it", True), ("Yes please, sir", True),
    ("No.", False), ("Nope", False), ("Don't do it", False), ("Never mind", False),
    ("Yes, no, wait", False),                      # any "no" wins: safety first
    ("What's the weather?", None), ("Um", None),
])
def test_yes_no(text, answer):
    assert classify_yes_no(text) is answer


def test_kill_switch_phrases():
    assert is_stand_down("Nova, stand down.") and is_stand_down("stand down")
    assert is_stand_down("Nova, stop everything")
    assert not is_stand_down("stand up comedy is great")
    assert is_resume("Nova, wake up") and is_resume("Nova, I need you")


def add_sleep_tool(registry, ran):
    @registry.tool("sleep_pc", "Put the PC to sleep.", risk=Risk.CONFIRM,
                   describe=lambda a: "put the PC to sleep")
    def sleep_pc(args, ctx):
        ran.append(True)
        return "Going to sleep."


def confirm_loop(settings, registry, answer_steps):
    ran = []
    add_sleep_tool(registry, ran)
    loop, events = make(settings, registry,
                        [tool_msg("sleep_pc", {}), text_msg("Very good, sir.")],
                        [("say", "Nova, put the PC to sleep", 10), ("quiet", 20),
                         ("until", lambda l: l._confirm is not None and "Shall I" in l._last_said),
                         *answer_steps], tts=FakeTTS())
    return loop, events, ran


async def test_spoken_yes_runs_the_action(settings, registry):
    loop, events, ran = confirm_loop(settings, registry, [("say", "yes", 10), ("quiet", 20)])
    await loop.run()
    assert ran == [True]
    assert "Shall I put the PC to sleep, sir?" in loop.tts.spoken
    assert any(e["type"] == "confirm_result" and e["approved"] for e in events)
    assert registry.audit.tail()[-1]["confirmed"] is True


async def test_spoken_no_declines(settings, registry):
    loop, events, ran = confirm_loop(settings, registry, [("say", "no", 10), ("quiet", 20)])
    await loop.run()
    assert ran == []
    assert registry.audit.tail()[-1]["status"] == "declined"


async def test_unclear_then_yes(settings, registry):
    loop, events, ran = confirm_loop(settings, registry, [
        ("say", "what's the weather", 10), ("quiet", 20),
        ("until", lambda l: "yes or a no" in l._last_said),
        ("say", "yes", 10), ("quiet", 20)])
    await loop.run()
    assert "Sorry, sir, was that a yes or a no?" in loop.tts.spoken
    assert ran == [True]


async def test_silence_times_out_as_no(settings, registry):
    settings.voice.confirm_timeout_s = 0.2
    loop, events, ran = confirm_loop(settings, registry, [("quiet", 200)])
    await loop.run()
    assert ran == []
    assert "I'll leave it, sir." in loop.tts.spoken


async def test_echo_of_the_question_is_not_an_answer(settings, registry):
    loop, events, ran = confirm_loop(settings, registry, [
        ("say", "shall I put the PC to sleep sir", 10), ("quiet", 20),
        ("say", "yes", 10), ("quiet", 20)])
    await loop.run()
    assert ran == [True]
    assert "Sorry, sir, was that a yes or a no?" not in loop.tts.spoken


async def test_stand_down_and_wake_up(settings, registry):
    loop, events = make(settings, registry, [STORY, text_msg("Noon, sir.")], [
        ("say", "Nova, tell me a story", 10), ("quiet", 20),
        ("until", lambda l: l.speaking),
        ("say", "Nova, stand down", 10), ("quiet", 20),
        ("until", lambda l: l.standby and not l.busy),
        ("say", "Nova, what time is it", 10), ("quiet", 20),      # ignored while standing down
        ("say", "Nova, wake up", 10), ("quiet", 20),
        ("until", lambda l: not l.standby and not l.busy),
        ("say", "Nova, what time is it", 10), ("quiet", 20)])
    await loop.run()
    kinds = [e["type"] for e in events]
    assert "interrupted" in kinds
    assert "The end." not in loop.tts.spoken
    assert "Standing down, sir." in loop.tts.spoken and "At your service, sir." in loop.tts.spoken
    assert any(e["type"] == "ignored" and "standing down" in e["reason"] for e in events)
    assert loop.tts.spoken[-1] == "Noon, sir."
    assert len(loop.brain.client.messages.calls) == 2          # story + time; nothing while down


async def test_stand_down_cancels_a_pending_confirmation(settings, registry):
    loop, events, ran = confirm_loop(settings, registry, [("say", "stand down", 10), ("quiet", 20)])
    await loop.run()
    assert ran == [] and loop.standby
