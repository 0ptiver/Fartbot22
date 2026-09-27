from pathlib import Path

import numpy as np
import pytest

from assistant.voice.stt.base import BufferedSession, STTProvider, Transcript
from assistant.voice.tts.base import TTSProvider

WAV = Path(__file__).parent / "data" / "what_time.wav"


def silero_or_skip():
    from assistant.voice.models import fetch_silero
    from assistant.voice.vad import SileroVAD

    try:
        return SileroVAD(fetch_silero())
    except Exception as e:  # offline and no cached model
        pytest.skip(f"Silero VAD model unavailable: {e}")


class FakeSTT(STTProvider):
    name = "fake"

    def __init__(self, text="what time is it"):
        self.text = text
        self.heard_seconds: list[float] = []

    def session(self):
        async def transcribe(audio):
            self.heard_seconds.append(len(audio) / 16000)
            return Transcript(self.text if len(audio) else "")
        return BufferedSession(transcribe)

    async def transcribe(self, audio):
        """Direct transcription (speculative STT and barge-in checks)."""
        self.direct_calls = getattr(self, "direct_calls", 0) + 1
        self.spec_seconds = getattr(self, "spec_seconds", []) + [len(audio) / 16000]
        return Transcript(self.text if len(audio) else "")


class FakeTTS(TTSProvider):
    name = "fake"
    sample_rate = 24000

    def __init__(self):
        self.spoken: list[str] = []

    async def synthesize(self, text):
        self.spoken.append(text)
        yield np.full(int(0.05 * self.sample_rate * max(1, len(text.split()))), 0.1, np.float32)
