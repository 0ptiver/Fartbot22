"""Audio I/O: microphone frames in, streamed playback out. Plus file-based stand-ins for tests."""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from typing import AsyncIterator

import numpy as np

from assistant.voice.vad import FRAME, SAMPLE_RATE


class MicStream:
    """Default (or chosen) microphone as 512-sample float32 frames at 16 kHz, timestamped."""

    def __init__(self, device: str | int | None = None, aec=None):
        self.device = device
        self.aec = aec                      # EchoCanceller: removes Nova's voice from the mic
        self._queue: asyncio.Queue[tuple[np.ndarray, float]] = asyncio.Queue(maxsize=500)
        self._stream = None
        self.overflows = 0

    def start(self) -> None:
        import sounddevice as sd

        loop = asyncio.get_running_loop()

        def callback(indata, frames, _time, status):
            if status.input_overflow:
                self.overflows += 1
            item = (indata[:, 0].copy(), time.perf_counter())
            loop.call_soon_threadsafe(self._put, item)

        self._stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                                      blocksize=FRAME, device=self.device, callback=callback)
        self._stream.start()

    def _put(self, item) -> None:
        if self._queue.full():  # never block the audio thread; drop the oldest frame
            self._queue.get_nowait()
        self._queue.put_nowait(item)

    async def frames(self) -> AsyncIterator[tuple[np.ndarray, float]]:
        if self._stream is None:
            self.start()
        while True:
            frame, t = await self._queue.get()
            if self.aec is not None:
                frame = self.aec.process(frame)
            yield frame, t

    def close(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None


class WavMic:
    """Plays a WAV file into the pipeline as if it were a microphone."""

    def __init__(self, path: str | Path, realtime: bool = False, tail_silence_s: float = 1.5):
        self.audio = load_wav_16k(path)
        self.realtime = realtime
        self.audio = np.concatenate([self.audio, np.zeros(int(tail_silence_s * SAMPLE_RATE), np.float32)])

    async def frames(self) -> AsyncIterator[tuple[np.ndarray, float]]:
        n = len(self.audio) // FRAME
        t0 = time.perf_counter()
        for i in range(n):
            frame = self.audio[i * FRAME:(i + 1) * FRAME]
            t_frame = t0 + (i + 1) * FRAME / SAMPLE_RATE
            if self.realtime:
                await asyncio.sleep(max(0.0, t_frame - time.perf_counter()))
            else:
                await asyncio.sleep(0)
            yield frame, (t_frame if self.realtime else time.perf_counter())

    def close(self) -> None:
        pass


def load_wav_16k(path: str | Path) -> np.ndarray:
    import soundfile as sf

    audio, sr = sf.read(str(path), dtype="float32", always_2d=True)
    audio = audio.mean(axis=1)
    if sr != SAMPLE_RATE:
        audio = resample(audio, sr, SAMPLE_RATE)
    return audio.astype(np.float32)


def resample(audio: np.ndarray, src: int, dst: int) -> np.ndarray:
    if src == dst:
        return audio
    n = int(round(len(audio) * dst / src))
    x_old = np.linspace(0, 1, len(audio), endpoint=False)
    x_new = np.linspace(0, 1, n, endpoint=False)
    return np.interp(x_new, x_old, audio).astype(np.float32)


class AudioPlayer:
    """Streams float32 chunks to the speakers. `stop()` cuts playback instantly (barge-in)."""

    def __init__(self, sample_rate: int = 24000, device: str | int | None = None, on_render=None):
        self.sample_rate = sample_rate
        self.device = device
        self.on_render = on_render          # gets every block actually sent to the speakers
        self._chunks: list[np.ndarray] = []
        self._offset = 0
        self._lock = threading.Lock()
        self._stream = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._drained = asyncio.Event()
        self._drained.set()
        self.first_audio_at: float | None = None

    def start(self) -> None:
        import sounddevice as sd

        self._loop = asyncio.get_running_loop()
        self._stream = sd.OutputStream(samplerate=self.sample_rate, channels=1, dtype="float32",
                                       device=self.device, callback=self._callback, blocksize=480,
                                       latency="low")
        self._stream.start()

    def _callback(self, outdata, frames, _time, _status) -> None:
        out = outdata[:, 0]
        filled = 0
        with self._lock:
            while filled < frames and self._chunks:
                chunk = self._chunks[0]
                take = min(frames - filled, len(chunk) - self._offset)
                out[filled:filled + take] = chunk[self._offset:self._offset + take]
                filled += take
                self._offset += take
                if self._offset >= len(chunk):
                    self._chunks.pop(0)
                    self._offset = 0
            empty = not self._chunks
        out[filled:] = 0
        if self.on_render is not None:
            try:
                self.on_render(out.copy())
            except Exception:  # never break audio output
                pass
        if filled and self.first_audio_at is None:
            self.first_audio_at = time.perf_counter()
        if empty and self._loop is not None:
            self._loop.call_soon_threadsafe(self._maybe_drained)

    def _maybe_drained(self) -> None:
        with self._lock:  # play() may have queued more audio since the callback ran
            empty = not self._chunks
        if empty:
            self._drained.set()

    def play(self, chunk: np.ndarray) -> None:
        if self._stream is None:
            self.start()
        with self._lock:
            self._chunks.append(chunk.astype(np.float32))
        self._drained.clear()

    @property
    def playing(self) -> bool:
        return not self._drained.is_set()

    def reset_marker(self) -> None:
        self.first_audio_at = None

    def stop(self) -> None:
        with self._lock:
            self._chunks.clear()
            self._offset = 0
        self._drained.set()

    async def drain(self) -> None:
        await self._drained.wait()

    def close(self) -> None:
        self.stop()
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None


class RecordingPlayer:
    """Collects audio instead of playing it (tests, --wav mode)."""

    def __init__(self, sample_rate: int = 24000):
        self.sample_rate = sample_rate
        self.chunks: list[np.ndarray] = []
        self.first_audio_at: float | None = None
        self.playing = False

    def play(self, chunk: np.ndarray) -> None:
        if self.first_audio_at is None:
            self.first_audio_at = time.perf_counter()
        self.chunks.append(chunk)

    def reset_marker(self) -> None:
        self.first_audio_at = None

    def stop(self) -> None:
        pass

    async def drain(self) -> None:
        pass

    def audio(self) -> np.ndarray:
        return np.concatenate(self.chunks) if self.chunks else np.zeros(0, np.float32)

    def save(self, path: str | Path) -> None:
        import soundfile as sf

        sf.write(str(path), self.audio(), self.sample_rate)

    def close(self) -> None:
        pass
