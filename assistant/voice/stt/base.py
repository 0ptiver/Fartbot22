"""Speech-to-text provider interface.

A provider opens one `STTSession` per utterance. Audio is fed while the user is
still talking (streaming providers transcribe as it arrives); `finish()` returns
the final transcript as soon as possible after end-of-speech.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

import numpy as np


@dataclass
class Transcript:
    text: str
    language: str | None = None


class STTSession(ABC):
    @abstractmethod
    async def feed(self, audio: np.ndarray) -> None:
        """Append float32 16 kHz mono audio."""

    @abstractmethod
    async def finish(self) -> Transcript:
        """No more audio; return the final transcript."""

    async def abort(self) -> None:
        """Discard the utterance."""


class STTProvider(ABC):
    name: str = "stt"

    async def load(self) -> None:
        """Load models / open connections. Called once at startup."""

    @abstractmethod
    def session(self) -> STTSession: ...


class BufferedSession(STTSession):
    """For batch recognizers: buffer audio, transcribe everything at finish()."""

    def __init__(self, transcribe):
        self._chunks: list[np.ndarray] = []
        self._transcribe = transcribe

    async def feed(self, audio: np.ndarray) -> None:
        self._chunks.append(audio)

    async def finish(self) -> Transcript:
        audio = np.concatenate(self._chunks) if self._chunks else np.zeros(0, np.float32)
        return await self._transcribe(audio)


def create_stt(settings) -> STTProvider:
    cfg = settings.voice.stt
    if cfg.provider == "whisper":
        from assistant.voice.stt.whisper import WhisperSTT
        return WhisperSTT(cfg.whisper)
    if cfg.provider == "deepgram":
        from assistant.voice.stt.deepgram import DeepgramSTT
        return DeepgramSTT(cfg.deepgram)
    raise ValueError(f"unknown STT provider: {cfg.provider}")
