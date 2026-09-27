"""Phase 3: talking over Nova, 'stop', pausing mid-request, speculative STT."""

import asyncio

import numpy as np

from assistant.brain.llm import Brain
from assistant.voice.audio import RecordingPlayer
from assistant.voice.pipeline import VoiceLoop
from tests.fakes import FakeClient, text_msg
from tests.voice_helpers import FakeSTT, FakeTTS

SPEECH, SILENCE = np.full(512, 0.3, np.float32), np.zeros(512, np.float32)


class SlowTTS(FakeTTS):
    """Takes a while per sentence so Nova is 'still talking' when the user interrupts."""

    def __init__(self, delay=0.15):
        super().__init__()
        self.delay = delay

    async def synthesize(self, text):
        self.spoken.append(text)
        await asyncio.sleep(self.delay)
        yield np.full(2400, 0.1, np.float32)


class ScriptMic:
    """Steps: ("say", text, frames) | ("quiet", frames) | ("until", predicate)."""

    def __init__(self, steps, loop_ref, frame_sleep=0.002):
        self.steps, self.ref, self.sleep = steps, loop_ref, frame_sleep

    async def frames(self):
        for step in self.steps:
            if step[0] == "until":
                for _ in range(2000):
                    if step[1](self.ref[0]):
                        break
                    await asyncio.sleep(0.005)
                continue
            if step[0] == "say":
                self.ref[0].stt.text = step[1]
            frame, n = (SPEECH, step[2]) if step[0] == "say" else (SILENCE, step[1])
            for _ in range(n):
                yield frame, 0.0
                await asyncio.sleep(self.sleep)
        while self.ref[0].busy:
            yield SILENCE, 0.0
            await asyncio.sleep(self.sleep)

    def close(self):
        pass


def make(settings, registry, script, steps, tts=None, delay=0.0):
    settings.voice.mode = "wake"
    settings.voice.vad.min_speech_ms = 32
    settings.voice.wake.cooldown_ms = 0
    events, ref = [], [None]
    brain = Brain(settings, registry, FakeClient(script, delay=delay))
    loop = VoiceLoop(settings, brain, FakeSTT(), tts or SlowTTS(), ScriptMic(steps, ref), RecordingPlayer(),
                     lambda f: 0.9 if np.max(np.abs(f)) > 0.1 else 0.0, None, events.append)
    ref[0] = loop
    return loop, events


STORY = text_msg("Once upon a time there was a fox. It lived in a wood. It liked cheese. The end.")


async def test_talking_over_nova_interrupts_and_answers(settings, registry):
    loop, events = make(settings, registry, [STORY, text_msg("It is noon, sir.")], [
        ("say", "Nova, tell me a story", 10), ("quiet", 20),
        ("until", lambda l: l.speaking),
        ("say", "what time is it", 40), ("quiet", 20)])
    await loop.run()
    kinds = [e["type"] for e in events]
    assert "interrupted" in kinds
    assert "It is noon, sir." in loop.tts.spoken
    assert "The end." not in loop.tts.spoken          # the story was cut off
    sent = loop.brain.client.messages.calls[1]["messages"][-1]["content"][-1]["text"]
    assert sent == "what time is it"


async def test_own_voice_does_not_interrupt(settings, registry):
    loop, events = make(settings, registry, [STORY], [
        ("say", "Nova, tell me a story", 10), ("quiet", 20),
        ("until", lambda l: l.speaking and "fox" in l._last_said),
        ("say", "once upon a time there was a fox", 40), ("quiet", 20)])
    await loop.run()
    assert "interrupted" not in [e["type"] for e in events]
    assert "The end." in loop.tts.spoken
    assert any(e["type"] == "ignored" and "own voice" in e["reason"] for e in events)


async def test_stop_word(settings, registry):
    loop, events = make(settings, registry, [STORY], [
        ("say", "Nova, tell me a story", 10), ("quiet", 20),
        ("until", lambda l: l.speaking),
        ("say", "stop", 30), ("quiet", 20)])
    await loop.run()
    kinds = [e["type"] for e in events]
    assert "interrupted" in kinds and "stopped" in kinds
    assert "The end." not in loop.tts.spoken
    assert len(loop.brain.client.messages.calls) == 1   # no new request


async def test_pause_and_continue_is_one_request(settings, registry):
    loop, events = make(settings, registry,
                        [text_msg("Sunny."), text_msg("Sunny in Chicago tomorrow, sir.")], [
        ("say", "Nova, what's the weather", 10), ("quiet", 16),
        ("until", lambda l: l.busy and not l.speaking),
        ("say", "in Chicago tomorrow", 10), ("quiet", 20)], delay=0.2)
    await loop.run()
    merged = [e for e in events if e["type"] == "merged"]
    assert merged and merged[0]["text"] == "What's the weather in Chicago tomorrow"
    last = loop.brain.client.messages.calls[-1]["messages"][-1]["content"][-1]["text"]
    assert last == "What's the weather in Chicago tomorrow"
    assert loop.tts.spoken[-1] == "Sunny in Chicago tomorrow, sir."


async def test_speculative_transcript_is_used(settings, registry):
    settings.voice.vad.pause_ms = 96
    loop, events = make(settings, registry, [text_msg("Noon, sir.")], [
        ("say", "Nova, what time is it", 10), ("quiet", 20)], tts=FakeTTS())
    await loop.run()
    assert loop.last_latency.marks.get("stt_speculative") == 1
    assert loop.stt.heard_seconds == []                # the end-of-speech session wasn't needed
    assert loop.tts.spoken == ["Noon, sir."]


def test_new_speech_rules(settings, registry):
    loop, _ = make(settings, registry, [], [])
    loop._last_said = "Once upon a time there was a fox. It lived in a wood."
    assert not loop._is_new_speech("once upon a time there was a fax")      # misheard echo
    assert not loop._is_new_speech("it lived in the wood")
    assert loop._is_new_speech("what time is it")                           # shares words, not phrases
    assert not loop._is_new_speech("fox")                                   # too short to judge
    assert loop._is_new_speech("stop")
    assert loop._is_new_speech("Nova, hang on")
    assert loop._stop_phrase("Nova, stop.") == "stop"
    assert loop._stop_phrase("never mind") == "never mind"
    assert loop._stop_phrase("stop the music please") is None       # a request, not "stop talking"
    assert loop._stop_phrase("okay stop now") == "okay stop"
    assert loop._stop_phrase("stop please") == "stop"
    assert loop._stop_phrase("stopwatch for ten minutes") is None


def test_echo_with_numbers_and_symbols(settings, registry):
    """The owner's case: Nova said "90 times 90 is 8,100." and Whisper heard "90 x 90", "8"."""
    loop, _ = make(settings, registry, [], [])
    loop._last_said = "90 times 90 is 8,100."
    assert loop._sounds_like_echo("90 x 90")
    assert loop._sounds_like_echo("ninety times ninety")
    assert loop._sounds_like_echo("8")
    assert not loop._is_new_speech("90 x 90")
    assert not loop._is_new_speech("8,100")
    assert loop._strip_echo_prefix("8") == ""
    assert loop._strip_echo_prefix("90 times 90, what about 12 times 12") == "what about 12 times 12"
    loop._last_said = "Once upon a time there was a fox. It lived in a wood."
    assert loop._is_new_speech("what time is it")
    assert loop._sounds_like_echo("once upon a time there was a fax")
