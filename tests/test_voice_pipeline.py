"""Integration: recorded WAV -> real Silero VAD -> STT -> brain -> chunker -> TTS -> player."""

import asyncio
import threading

import numpy as np

from assistant.brain.llm import Brain
from assistant.voice.audio import RecordingPlayer, WavMic
from assistant.voice.latency import LatencyTracker
from assistant.voice.pipeline import VoiceLoop
from tests.fakes import FakeClient, text_msg, tool_msg
from tests.voice_helpers import WAV, FakeSTT, FakeTTS, silero_or_skip


def make_loop(settings, registry, script, mic, vad, ptt=None):
    events = []
    brain = Brain(settings, registry, FakeClient(script))
    stt, tts, player = FakeSTT(), FakeTTS(), RecordingPlayer()
    loop = VoiceLoop(settings, brain, stt, tts, mic, player, vad, ptt, events.append)
    return loop, events, stt, tts, player


async def test_open_mic_turn_from_wav(settings, registry):
    settings.voice.mode = "open_mic"
    vad = silero_or_skip()
    script = [tool_msg("get_time", {}, text="One moment, sir."),
              text_msg("It is a quarter past three. Anything else?")]
    loop, events, stt, tts, player = make_loop(settings, registry, script, WavMic(WAV), vad)
    await loop.run(max_turns=1)

    kinds = [e["type"] for e in events]
    assert kinds[0] == "listening"
    assert "transcript" in kinds and "tool" in kinds and "latency" in kinds
    # VAD cut a ~2 s utterance (plus pre-roll and trailing silence) out of the 3 s clip.
    assert 1.5 < stt.heard_seconds[0] < 3.0
    # Spoken in chunks, with the pre-tool acknowledgement first.
    assert tts.spoken == ["One moment, sir.", "It is a quarter past three.", "Anything else?"]
    assert len(player.audio()) > 0
    lat = loop.last_latency
    for m in ("speech_end", "eot_detected", "stt_done", "llm_first_token", "first_chunk",
              "tts_first_audio", "playback_start"):
        assert m in lat.marks, m
    assert [m["role"] for m in loop.conv.messages] == ["user", "assistant", "user", "assistant"]


class FakePTT:
    def __init__(self):
        self.held = threading.Event()


class ScriptedMic:
    """Yields frames and toggles the PTT key on a schedule."""

    def __init__(self, ptt, press_at, release_at, total):
        self.ptt, self.press_at, self.release_at, self.total = ptt, press_at, release_at, total

    async def frames(self):
        for i in range(self.total):
            if i == self.press_at:
                self.ptt.held.set()
            if i == self.release_at:
                self.ptt.held.clear()
            yield np.zeros(512, np.float32), float(i)
            await asyncio.sleep(0)

    def close(self):
        pass


async def test_push_to_talk(settings, registry):
    settings.voice.mode = "ptt"
    ptt = FakePTT()
    mic = ScriptedMic(ptt, press_at=5, release_at=36, total=60)
    loop, events, stt, tts, player = make_loop(
        settings, registry, [text_msg("Good afternoon, sir.")], mic, lambda f: 0.0, ptt)
    await loop.run(max_turns=1)
    assert stt.heard_seconds == [31 * 512 / 16000]
    assert tts.spoken == ["Good afternoon, sir."]


async def test_empty_transcript_is_ignored(settings, registry):
    settings.voice.mode = "ptt"
    ptt = FakePTT()
    mic = ScriptedMic(ptt, press_at=2, release_at=3, total=10)
    loop, events, stt, tts, player = make_loop(settings, registry, [], mic, lambda f: 0.0, ptt)
    stt.text = ""
    await loop.run(max_turns=1)
    assert tts.spoken == [] and any(e.get("reason") == "heard nothing" for e in events)


def test_latency_report():
    lat = LatencyTracker()
    for i, m in enumerate(["speech_end", "eot_detected", "stt_done", "llm_first_token",
                           "first_chunk", "tts_first_audio", "playback_start"]):
        lat.mark(m, i * 0.1)
    assert lat.total_ms() == 600
    assert "OK" in lat.report(800) and "OVER" in lat.report(500)


async def test_filler_before_slow_tool(settings, registry):
    settings.voice.mode = "ptt"
    settings.voice.filler_phrases = ["One moment, sir."]
    ptt = FakePTT()
    mic = ScriptedMic(ptt, press_at=2, release_at=10, total=20)
    script = [tool_msg("escalate", {"task": "x"}), text_msg("Done.")]
    settings.brain.expert.backend = "anthropic"
    loop, events, stt, tts, player = make_loop(settings, registry, script, mic, lambda f: 0.0, ptt)
    loop.brain.expert = type("E", (), {"ask": staticmethod(lambda *a, **k: _answer())})()
    await loop.run(max_turns=1)
    assert tts.spoken == ["One moment, sir.", "Done."]


async def _answer():
    return "expert answer"
