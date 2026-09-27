"""Text-to-speech provider interface: text in, a stream of float32 audio chunks out."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import AsyncIterator

import numpy as np


class TTSProvider(ABC):
    name: str = "tts"
    sample_rate: int = 24000

    async def load(self) -> None:
        """Load models / warm up. Called once at startup."""

    @abstractmethod
    def synthesize(self, text: str) -> AsyncIterator[np.ndarray]:
        """Yield mono float32 chunks at `self.sample_rate` as soon as each is ready."""


def create_tts(settings) -> TTSProvider:
    cfg = settings.voice.tts
    if cfg.provider == "kokoro":
        from assistant.voice.tts.kokoro import KokoroTTS
        return KokoroTTS(cfg.kokoro)
    raise ValueError(f"unknown TTS provider: {cfg.provider}")
