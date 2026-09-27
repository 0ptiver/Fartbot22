"""Local STT with faster-whisper (CTranslate2). GPU if available, CPU fallback."""

from __future__ import annotations

import asyncio
import logging

import numpy as np

from assistant.core.config import WhisperConfig
from assistant.voice.stt.base import BufferedSession, STTProvider, STTSession, Transcript

log = logging.getLogger(__name__)


class WhisperSTT(STTProvider):
    name = "whisper"

    def __init__(self, cfg: WhisperConfig):
        self.cfg = cfg
        self.model = None
        self.device = None
        # CTranslate2 isn't safe for concurrent calls on one model; serialize.
        self._lock = asyncio.Lock()

    async def load(self) -> None:
        await asyncio.to_thread(self._load)

    def _load(self) -> None:
        from assistant.voice.cuda import add_nvidia_dll_dirs

        add_nvidia_dll_dirs()
        from faster_whisper import WhisperModel

        attempts = []
        if self.cfg.device in ("auto", "cuda"):
            ct = "int8_float16" if self.cfg.compute_type == "auto" else self.cfg.compute_type
            attempts.append(("cuda", ct))
        if self.cfg.device in ("auto", "cpu"):
            ct = "int8" if self.cfg.compute_type == "auto" else self.cfg.compute_type
            attempts.append(("cpu", ct))
        last_err: Exception | None = None
        for device, compute_type in attempts:
            try:
                model = WhisperModel(self.cfg.model, device=device, compute_type=compute_type)
                # Warm-up: the first CUDA call is slow; pay it at startup, not on your first sentence.
                list(model.transcribe(np.zeros(16000, np.float32), beam_size=1, language="en")[0])
                self.model, self.device = model, f"{device}/{compute_type}"
                log.info("whisper %s loaded on %s", self.cfg.model, self.device)
                return
            except Exception as e:  # CUDA missing/unsupported -> try the next option
                log.warning("whisper on %s failed: %s", device, e)
                last_err = e
        raise RuntimeError(f"Could not load Whisper model {self.cfg.model}: {last_err}")

    def session(self) -> STTSession:
        return BufferedSession(self.transcribe)

    async def transcribe(self, audio: np.ndarray) -> Transcript:
        if self.model is None:
            await self.load()
        if audio.size < 1600:  # < 0.1 s: nothing to hear
            return Transcript("")
        async with self._lock:
            return await asyncio.to_thread(self._transcribe, audio)

    def _transcribe(self, audio: np.ndarray) -> Transcript:
        audio = normalize(audio)
        segments, info = self.model.transcribe(
            audio,
            beam_size=self.cfg.beam_size,
            language=self.cfg.language,
            condition_on_previous_text=False,
            without_timestamps=True,
            vad_filter=False,  # we already ran Silero VAD
            hotwords=self.cfg.hotwords,
        )
        # Whisper's own rule: only drop a segment when it's both "probably silence"
        # and low-confidence. (Dropping on no_speech_prob alone loses quiet speech.)
        text = " ".join(s.text.strip() for s in segments
                        if not (s.no_speech_prob > 0.6 and s.avg_logprob < -1.0)).strip()
        return Transcript(_clean(text), info.language)


def normalize(audio: np.ndarray, target_peak: float = 0.5, max_gain: float = 30.0) -> np.ndarray:
    """Boost quiet microphones so Whisper can hear them (never amplifies pure silence)."""
    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    if peak < 1e-4 or peak >= target_peak:
        return audio
    return (audio * min(target_peak / peak, max_gain)).astype(np.float32)


# Whisper hallucinates these on silence / noise.
_HALLUCINATIONS = {"thank you.", "thanks for watching!", "thanks for watching.", "you", "bye.", "."}


def _clean(text: str) -> str:
    return "" if text.lower() in _HALLUCINATIONS else text
