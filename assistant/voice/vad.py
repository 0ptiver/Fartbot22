"""Voice activity detection (Silero VAD via onnxruntime) and end-of-turn detection."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from assistant.core.config import VADConfig

SAMPLE_RATE = 16000
FRAME = 512                      # Silero v5 expects 512 samples (32 ms) at 16 kHz
FRAME_MS = FRAME * 1000 / SAMPLE_RATE
_CONTEXT = 64


class SileroVAD:
    """Streaming Silero VAD without torch: feed 512-sample float32 frames, get P(speech)."""

    def __init__(self, model_path: str | Path):
        import onnxruntime as ort

        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        self.session = ort.InferenceSession(str(model_path), sess_options=opts,
                                            providers=["CPUExecutionProvider"])
        self.reset()

    def reset(self) -> None:
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros((1, _CONTEXT), dtype=np.float32)

    def __call__(self, frame: np.ndarray) -> float:
        if frame.shape[-1] != FRAME:
            raise ValueError(f"VAD frames must be {FRAME} samples, got {frame.shape[-1]}")
        x = np.concatenate([self._context, frame.reshape(1, -1).astype(np.float32)], axis=1)
        out, self._state = self.session.run(
            None, {"input": x, "state": self._state, "sr": np.array(SAMPLE_RATE, dtype=np.int64)}
        )
        self._context = x[:, -_CONTEXT:]
        return float(out[0][0])


@dataclass
class SpeechStart:
    t: float                     # stream time (s) of the first speech frame


@dataclass
class SpeechPause:
    audio: np.ndarray            # utterance so far: transcribe it early (speculative STT)


@dataclass
class SpeechEnd:
    audio: np.ndarray            # the whole utterance incl. pre-roll, float32 16 kHz
    t_last_speech: float         # stream time the user actually stopped talking
    t_detected: float            # stream time we decided the turn was over
    truncated: bool = False
    silent_since_pause: bool = False   # no speech after the last SpeechPause: its audio is final


class Endpointer:
    """Turns per-frame speech probabilities into utterances.

    Pure logic (no audio I/O), so it is unit-testable with synthetic probabilities.
    """

    def __init__(self, cfg: VADConfig):
        self.cfg = cfg
        self.preroll_frames = max(1, int(cfg.preroll_ms / FRAME_MS))
        self.min_speech_frames = max(1, int(cfg.min_speech_ms / FRAME_MS))
        self.end_frames = max(1, int(cfg.end_silence_ms / FRAME_MS))
        self.pause_frames = max(1, int(cfg.pause_ms / FRAME_MS)) if cfg.pause_ms else 0
        self.max_frames = int(cfg.max_utterance_s * 1000 / FRAME_MS)
        self.reset()

    def reset(self) -> None:
        self._pre: deque[np.ndarray] = deque(maxlen=self.preroll_frames)
        self._buf: list[np.ndarray] = []
        self._in_speech = False
        self._speech_run = 0       # consecutive speech frames before start is confirmed
        self._silence_run = 0
        self._voiced = 0
        self._t_last_speech = 0.0
        self._t_start = 0.0
        self._paused = False

    @property
    def in_speech(self) -> bool:
        return self._in_speech

    @property
    def speech_seconds(self) -> float:
        return self._voiced * FRAME_MS / 1000

    def audio(self) -> np.ndarray:
        return np.concatenate(self._buf) if self._buf else np.zeros(0, np.float32)

    def process(self, frame: np.ndarray, prob: float, t: float) -> SpeechStart | SpeechPause | SpeechEnd | None:
        """`t` is the stream time (seconds) at the *end* of this frame."""
        is_speech = prob >= self.cfg.threshold
        if not self._in_speech:
            self._pre.append(frame)
            if is_speech:
                self._speech_run += 1
                if self._speech_run == 1:
                    self._t_start = t - FRAME_MS / 1000
                if self._speech_run >= self.min_speech_frames:
                    self._in_speech = True
                    self._buf = list(self._pre)
                    self._silence_run = 0
                    self._voiced = self._speech_run
                    self._t_last_speech = t
                    return SpeechStart(self._t_start)
            else:
                self._speech_run = 0
            return None

        self._buf.append(frame)
        # Hysteresis: once talking, a slightly lower probability still counts as speech.
        if prob >= self.cfg.threshold - 0.15:
            self._silence_run = 0
            self._voiced += 1
            self._t_last_speech = t
            self._paused = False
        else:
            self._silence_run += 1
        if self._silence_run >= self.end_frames or len(self._buf) >= self.max_frames:
            return self._finish(t, truncated=len(self._buf) >= self.max_frames)
        if self.pause_frames and self._silence_run == self.pause_frames:
            self._paused = True
            return SpeechPause(self.audio())
        return None

    def force_end(self, t: float) -> SpeechEnd | None:
        """End the utterance now (push-to-talk released)."""
        if not self._in_speech:
            self.reset()
            return None
        return self._finish(t)

    def _finish(self, t: float, truncated: bool = False) -> SpeechEnd:
        end = SpeechEnd(self.audio(), self._t_last_speech, t, truncated, self._paused)
        self.reset()
        return end
