"""Acoustic echo cancellation: remove Nova's own voice from the microphone signal.

Uses WebRTC's audio processing module (AEC3, the echo canceller used in video calls) via the
`livekit` package. The speaker output is fed in as the "far end" reference; the mic signal
is cleaned before voice detection and Whisper ever see it, so Nova doesn't hear itself.
"""

from __future__ import annotations

import logging
import threading

import numpy as np

log = logging.getLogger(__name__)

RATE = 16000
CHUNK = 160          # WebRTC processes exactly 10 ms at a time


def _to_int16(x: np.ndarray) -> np.ndarray:
    return (np.clip(x, -1.0, 1.0) * 32767).astype(np.int16)


def _resample(x: np.ndarray, src: int, dst: int) -> np.ndarray:
    if src == dst or not len(x):
        return x.astype(np.float32)
    n = int(round(len(x) * dst / src))
    return np.interp(np.linspace(0, len(x), n, endpoint=False), np.arange(len(x)), x).astype(np.float32)


class EchoCanceller:
    def __init__(self, delay_ms: int = 60, noise_suppression: bool = False):
        from livekit import rtc

        self._rtc = rtc
        self.apm = rtc.AudioProcessingModule(echo_cancellation=True, high_pass_filter=True,
                                             noise_suppression=noise_suppression)
        self.apm.set_stream_delay_ms(int(delay_ms))
        self.delay_ms = int(delay_ms)
        self._lock = threading.Lock()
        self._rev_buf = np.zeros(0, np.float32)
        self._in_buf = np.zeros(0, np.float32)
        self._out_buf = np.zeros(0, np.float32)
        self._rev_carry = np.zeros(0, np.float32)   # resampling remainder
        self.reverse_frames = 0

    def _frame(self, samples: np.ndarray):
        frame = self._rtc.AudioFrame.create(RATE, 1, CHUNK)
        np.frombuffer(frame.data, dtype=np.int16)[:] = _to_int16(samples)
        return frame

    # --- far end: what the speakers are playing (called from the audio output thread) ---
    def feed_reverse(self, audio: np.ndarray, sample_rate: int) -> None:
        x = _resample(np.asarray(audio, np.float32).ravel(), sample_rate, RATE)
        with self._lock:
            self._rev_buf = np.concatenate([self._rev_buf, x])
            while len(self._rev_buf) >= CHUNK:
                chunk, self._rev_buf = self._rev_buf[:CHUNK], self._rev_buf[CHUNK:]
                self.apm.process_reverse_stream(self._frame(chunk))
                self.reverse_frames += 1

    # --- near end: the microphone ------------------------------------------------------
    def process(self, mic: np.ndarray) -> np.ndarray:
        """Clean a block of 16 kHz mic audio. Returns the same number of samples (a 10 ms
        processing delay is absorbed by keeping a small buffer)."""
        with self._lock:
            self._in_buf = np.concatenate([self._in_buf, np.asarray(mic, np.float32).ravel()])
            while len(self._in_buf) >= CHUNK:
                chunk, self._in_buf = self._in_buf[:CHUNK], self._in_buf[CHUNK:]
                frame = self._frame(chunk)
                self.apm.process_stream(frame)
                cleaned = np.frombuffer(frame.data, dtype=np.int16).astype(np.float32) / 32767
                self._out_buf = np.concatenate([self._out_buf, cleaned])
            n = len(mic)
            if len(self._out_buf) >= n:
                out, self._out_buf = self._out_buf[:n], self._out_buf[n:]
            else:  # only at start-up: pad the first block
                out = np.concatenate([np.zeros(n - len(self._out_buf), np.float32), self._out_buf])
                self._out_buf = np.zeros(0, np.float32)
        return out


    def close(self) -> None:
        """Release the native module now, while its runtime is still alive (avoids a noisy
        but harmless error from livekit when Python exits)."""
        import gc

        with self._lock:
            self.apm = None
        gc.collect()


def create_echo_canceller(cfg) -> EchoCanceller | None:
    if not cfg.enabled:
        return None
    try:
        return EchoCanceller(cfg.delay_ms, cfg.noise_suppression)
    except Exception as e:  # livekit missing or failed to load: carry on without it
        log.warning("echo cancellation unavailable: %s", e)
        return None
