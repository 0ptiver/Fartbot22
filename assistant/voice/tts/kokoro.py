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
        from assistant.voice.voicedesign import VoiceDesign

        self.cfg = cfg
        self.kokoro = None
        self.device: str | None = None
        # Your saved voice (data/voice.json) wins over config.yaml's voice and speed.
        self.design = VoiceDesign.load(VoiceDesign({cfg.voice: 1.0}, cfg.speed, 0.0, cfg.lang))
        self._style = None

    async def load(self) -> None:
        await asyncio.to_thread(self._load)

    def _load(self) -> None:
        from kokoro_onnx import Kokoro

        logging.getLogger("phonemizer").setLevel(logging.ERROR)  # harmless "words count mismatch"
        model, voices = kokoro_paths(resolve_model_file(self.cfg))
        if not model.exists() or not voices.exists():
            raise RuntimeError("Kokoro model files missing. Run: python -m assistant models")
        session = make_session(model, self.cfg.device, self.cfg.threads, self.cfg.cuda_conv_search)
        self.kokoro = Kokoro.from_session(session, str(voices))
        self._apply(self.design)
        for _ in range(2):  # warm-up: the first runs are slower
            self._render("Ready, sir.", self.design, self._style)
        self.device = "cuda" if session.get_providers()[0] == "CUDAExecutionProvider" else "cpu"
        log.info("kokoro loaded (voice %s, %s)", self.design.mix, self.device)

    # --- voice design --------------------------------------------------------------------------
    def voices(self) -> list[str]:
        return list(self.kokoro.get_voices()) if self.kokoro is not None else []

    def _apply(self, design) -> None:
        from assistant.voice.voicedesign import blend

        design = design.clean(self.voices() or None)
        self._style = blend(self.kokoro.get_voice_style, design.mix)
        self.design = design

    def _render(self, text: str, design, style) -> np.ndarray:
        from assistant.voice.voicedesign import pitch_factor, repitch

        f = pitch_factor(design.pitch)
        audio, sr = self.kokoro.create(text, voice=style, speed=design.speed / f, lang=design.lang, trim=True)
        if sr != self.sample_rate:
            raise RuntimeError(f"unexpected Kokoro sample rate {sr}")
        return repitch(audio.astype(np.float32), design.pitch)

    async def set_design(self, design) -> None:
        """Use a new voice from now on."""
        if self.kokoro is None:
            await self.load()
        await asyncio.to_thread(self._apply, design)

    async def preview(self, design, text: str) -> np.ndarray:
        """How `design` sounds, without switching to it."""
        from assistant.voice.voicedesign import blend

        if self.kokoro is None:
            await self.load()
        d = design.clean(self.voices() or None)
        return await asyncio.to_thread(self._render, text, d, blend(self.kokoro.get_voice_style, d.mix))

    async def synthesize(self, text: str) -> AsyncIterator[np.ndarray]:
        if self.kokoro is None:
            await self.load()
        # The chunker already hands us one sentence/clause at a time, so a single
        # create() per chunk gives the lowest time-to-first-audio.
        yield await asyncio.to_thread(self._render, text, self.design, self._style)


def resolve_model_file(cfg: KokoroConfig) -> str:
    from assistant.voice.models import KOKORO_FILES, KOKORO_GPU, MODELS_DIR

    if cfg.model_file != "auto":
        return cfg.model_file
    if cfg.device != "cpu" and (MODELS_DIR / KOKORO_GPU).exists() and cuda_available():
        return KOKORO_GPU
    return KOKORO_FILES[0]


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


def make_session(model_path, device: str = "auto", threads: int | None = None,
                 conv_search: str = "HEURISTIC"):
    """ONNX session for Kokoro. GPU when available (much faster); on CPU, limiting threads
    to the performance cores is often faster on Intel hybrid chips than using every core.

    conv_search: onnxruntime's default (EXHAUSTIVE) benchmarks cuDNN algorithms for every
    new input shape. Every sentence has a new length, so HEURISTIC is far faster for speech."""
    import onnxruntime as ort

    ort.set_default_logger_severity(3)  # hide harmless kernel warnings (e.g. ScatterND)
    opts = ort.SessionOptions()
    opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    opts.log_severity_level = 3
    if threads:
        opts.intra_op_num_threads = threads
    providers = ["CPUExecutionProvider"]
    if device in ("auto", "cuda") and cuda_available():
        providers = [("CUDAExecutionProvider", {"cudnn_conv_algo_search": conv_search.upper()}),
                     "CPUExecutionProvider"]
    elif device == "cuda":
        log.warning("Kokoro: CUDA requested but not available; using CPU")
    return ort.InferenceSession(str(model_path), sess_options=opts, providers=providers)
