"""Local TTS with Kokoro-82M (ONNX). ~100 ms to first audio for a short clause."""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator

import numpy as np

from assistant.core.config import KokoroConfig
from assistant.voice.models import kokoro_paths
from assistant.voice.tts.base import TTSProvider

log = logging.getLogger(__name__)


class KokoroTTS(TTSProvider):
    name = "kokoro"
    sample_rate = 24000

    def __init__(self, cfg: KokoroConfig):
        self.cfg = cfg
        self.kokoro = None

    async def load(self) -> None:
        await asyncio.to_thread(self._load)

    def _load(self) -> None:
        import os

        # Keep Kokoro on the CPU by default so the GPU is free for Whisper.
        provider = "CUDAExecutionProvider" if self.cfg.device == "cuda" else "CPUExecutionProvider"
        os.environ.setdefault("ONNX_PROVIDER", provider)
        from kokoro_onnx import Kokoro

        model, voices = kokoro_paths()
        if not model.exists() or not voices.exists():
            raise RuntimeError("Kokoro model files missing. Run: python -m assistant models")
        self.kokoro = Kokoro(str(model), str(voices))
        self.kokoro.create("Ready.", voice=self.cfg.voice, speed=self.cfg.speed, lang=self.cfg.lang)
        log.info("kokoro loaded (voice %s)", self.cfg.voice)

    async def synthesize(self, text: str) -> AsyncIterator[np.ndarray]:
        if self.kokoro is None:
            await self.load()
        # The chunker already hands us one sentence/clause at a time, so a single
        # create() per chunk gives the lowest time-to-first-audio.
        audio, sr = await asyncio.to_thread(
            self.kokoro.create, text, voice=self.cfg.voice, speed=self.cfg.speed,
            lang=self.cfg.lang, trim=True,
        )
        if sr != self.sample_rate:
            raise RuntimeError(f"unexpected Kokoro sample rate {sr}")
        yield audio.astype(np.float32)
