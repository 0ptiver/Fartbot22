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
        self.device: str | None = None

    async def load(self) -> None:
        await asyncio.to_thread(self._load)

    def _load(self) -> None:
        from kokoro_onnx import Kokoro

        logging.getLogger("phonemizer").setLevel(logging.ERROR)  # harmless "words count mismatch"
        model, voices = kokoro_paths(self.cfg.model_file)
        if not model.exists() or not voices.exists():
            raise RuntimeError("Kokoro model files missing. Run: python -m assistant models")
        session = make_session(model, self.cfg.device, self.cfg.threads)
        self.kokoro = Kokoro.from_session(session, str(voices))
        for _ in range(2):  # warm-up: the first runs are slower
            self.kokoro.create("Ready, sir.", voice=self.cfg.voice, speed=self.cfg.speed, lang=self.cfg.lang)
        self.device = "cuda" if session.get_providers()[0] == "CUDAExecutionProvider" else "cpu"
        log.info("kokoro loaded (voice %s, %s)", self.cfg.voice, self.device)

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


def cuda_available() -> bool:
    """True when onnxruntime-gpu is installed and its CUDA libraries load."""
    import onnxruntime as ort

    if "CUDAExecutionProvider" not in ort.get_available_providers():
        return False
    if hasattr(ort, "preload_dlls"):
        try:  # loads CUDA/cuDNN from the pip-installed nvidia packages on Windows
            ort.preload_dlls()
        except Exception as e:
            log.warning("onnxruntime CUDA libraries didn't load: %s", e)
            return False
    return True


def make_session(model_path, device: str = "auto", threads: int | None = None):
    """ONNX session for Kokoro. GPU when available (much faster); on CPU, limiting threads
    to the performance cores is often faster on Intel hybrid chips than using every core."""
    import onnxruntime as ort

    opts = ort.SessionOptions()
    opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    opts.log_severity_level = 3
    if threads:
        opts.intra_op_num_threads = threads
    providers = ["CPUExecutionProvider"]
    if device in ("auto", "cuda") and cuda_available():
        providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]
    elif device == "cuda":
        log.warning("Kokoro: CUDA requested but not available; using CPU")
    return ort.InferenceSession(str(model_path), sess_options=opts, providers=providers)
