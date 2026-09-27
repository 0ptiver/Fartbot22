import numpy as np
import pytest

from assistant.core.config import VADConfig
from assistant.voice.audio import load_wav_16k
from assistant.voice.vad import FRAME, Endpointer, SpeechEnd, SpeechStart

from tests.voice_helpers import WAV, silero_or_skip


def feed(ep, probs, frame_s=0.032):
    events = []
    for i, p in enumerate(probs):
        ev = ep.process(np.full(FRAME, i, np.float32), p, (i + 1) * frame_s)
        if ev:
            events.append((i, ev))
    return events


def test_endpointer_basic_utterance():
    ep = Endpointer(VADConfig(end_silence_ms=320, min_speech_ms=64, preroll_ms=96, pause_ms=0))
    probs = [0.0] * 10 + [0.9] * 20 + [0.0] * 15
    events = feed(ep, probs)
    (i0, start), (i1, end) = events
    assert isinstance(start, SpeechStart) and i0 == 11
    assert isinstance(end, SpeechEnd)
    assert i1 == 29 + 10  # 10 silent frames = 320 ms
    # utterance = 3 pre-roll frames (incl. the 2 speech frames) + rest
    assert end.audio[0] == 9 and len(end.audio) == FRAME * (i1 - 9 + 1)
    assert end.t_last_speech == pytest.approx(30 * 0.032)


def test_endpointer_ignores_blips_and_bridges_short_pauses():
    ep = Endpointer(VADConfig(end_silence_ms=320, min_speech_ms=96, pause_ms=0))
    assert feed(ep, [0.0, 0.9, 0.0, 0.9, 0.9, 0.0] + [0.0] * 20) == []
    ep.reset()
    probs = [0.9] * 10 + [0.0] * 5 + [0.9] * 10 + [0.0] * 12
    events = feed(ep, probs)
    assert [type(e) for _, e in events] == [SpeechStart, SpeechEnd]


def test_endpointer_max_length():
    ep = Endpointer(VADConfig(max_utterance_s=1.0, min_speech_ms=32))
    events = feed(ep, [0.9] * 100)
    ends = [e for _, e in events if isinstance(e, SpeechEnd)]
    assert ends and ends[0].truncated


def test_force_end():
    ep = Endpointer(VADConfig(min_speech_ms=32))
    feed(ep, [0.9] * 5)
    end = ep.force_end(1.0)
    assert isinstance(end, SpeechEnd) and not ep.in_speech
    assert ep.force_end(2.0) is None


def test_silero_on_real_speech():
    vad = silero_or_skip()
    audio = load_wav_16k(WAV)
    probs = [vad(audio[i:i + FRAME]) for i in range(0, len(audio) - FRAME, FRAME)]
    t = np.arange(len(probs)) * FRAME / 16000
    speech = t[np.array(probs) > 0.5]
    assert len(speech) > 20
    # Clip = 0.5 s silence, ~2 s of speech (incl. espeak padding), 0.5 s silence.
    assert 0.4 <= speech.min() <= 1.0
    assert 1.5 <= speech.max() <= 2.6
    assert max(probs[:10]) < 0.3  # leading silence is silence


def test_pause_events_for_speculative_stt():
    ep = Endpointer(VADConfig(end_silence_ms=320, min_speech_ms=32, pause_ms=160))
    # speech, short pause (spec fires), more speech (spec invalid), long pause (spec fires, end clean)
    probs = [0.9] * 10 + [0.0] * 6 + [0.9] * 5 + [0.0] * 12
    events = feed(ep, probs)
    kinds = [type(e).__name__ for _, e in events]
    assert kinds == ["SpeechStart", "SpeechPause", "SpeechPause", "SpeechEnd"]
    first_pause, second_pause, end = events[1][1], events[2][1], events[3][1]
    assert len(second_pause.audio) > len(first_pause.audio)
    assert end.silent_since_pause is True


def test_end_without_clean_pause():
    ep = Endpointer(VADConfig(max_utterance_s=0.5, min_speech_ms=32, pause_ms=160))
    events = feed(ep, [0.9] * 40)
    end = [e for _, e in events if isinstance(e, SpeechEnd)][0]
    assert end.truncated and end.silent_since_pause is False
