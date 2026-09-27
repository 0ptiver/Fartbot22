"""Live subtitles: what the PC is playing (game voice chat, videos), captioned and translated.

    PC sound (not the mic) --WASAPI loopback--> 16 kHz --Silero VAD--> speech segments
      --Whisper (any language)--> caption --local model--> English, if it wasn't English

Captions are shown in a see-through box near the bottom of the screen (clicks pass
through) and in the HUD. They're other people's words, so they're never saved or logged.
Nova's own voice is skipped (it also comes out of the speakers).
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any, Awaitable, Callable

import numpy as np

log = logging.getLogger(__name__)

RATE = 16000
FRAME = 512                      # Silero wants 32 ms frames at 16 kHz
# Whisper's favourite hallucinations on music or noise.
_JUNK = {"thank you", "thanks for watching", "thank you for watching", "you", "bye", "subtitles by",
         "please subscribe", "♪", "music", "[music]", "(music)"}


class LoopbackCapture:
    """Records what the default speakers are playing. Runs in its own thread."""

    def __init__(self, block_s: float = 0.1):
        self.block_s = block_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self, on_audio: Callable[[np.ndarray], None]) -> None:
        self._stop.clear()
        self._error: Exception | None = None
        ready = threading.Event()

        def run() -> None:
            try:
                import soundcard as sc   # imported here: it sets up Windows audio for this thread
                speaker = sc.default_speaker()
                mic = sc.get_microphone(id=str(speaker.name), include_loopback=True)
                sr = 48000
                with mic.recorder(samplerate=sr, channels=2, blocksize=int(sr * self.block_s)) as rec:
                    ready.set()
                    while not self._stop.is_set():
                        data = rec.record(numframes=int(sr * self.block_s))
                        on_audio(to_16k_mono(np.asarray(data, np.float32), sr))
            except Exception as e:
                self._error = e
                log.warning("subtitles: can't record the PC's sound: %s", e)
                ready.set()

        self._thread = threading.Thread(target=run, name="subtitles-capture", daemon=True)
        self._thread.start()
        ready.wait(5)
        if self._error is not None:
            raise RuntimeError(f"I can't hear the PC's sound ({self._error}).")

    def stop(self) -> None:
        self._stop.set()


def to_16k_mono(block: np.ndarray, sr: int) -> np.ndarray:
    mono = block.mean(axis=1) if block.ndim == 2 else block
    if sr == RATE:
        return mono.astype(np.float32)
    if sr % RATE == 0:                                   # 48 kHz: average each group of 3
        k = sr // RATE
        n = mono.size // k * k
        return mono[:n].reshape(-1, k).mean(axis=1).astype(np.float32)
    x = np.linspace(0, mono.size - 1, int(mono.size * RATE / sr))
    return np.interp(x, np.arange(mono.size), mono).astype(np.float32)


class Segmenter:
    """Speech in, finished (and in-progress) segments out."""

    def __init__(self, vad: Callable[[np.ndarray], float], threshold: float = 0.5, end_silence_s: float = 0.6,
                 max_s: float = 8.0, partial_every_s: float = 1.5):
        self.vad, self.th = vad, threshold
        self.end_frames = int(end_silence_s * RATE / FRAME)
        self.max_samples = int(max_s * RATE)
        self.partial_every = int(partial_every_s * RATE)
        self._pending = np.zeros(0, np.float32)
        self._speech: list[np.ndarray] = []
        self._silent = 0
        self._since_partial = 0

    def feed(self, audio: np.ndarray) -> list[tuple[str, np.ndarray]]:
        """Returns ('partial' | 'final', audio) events."""
        out = []
        self._pending = np.concatenate([self._pending, audio])
        while self._pending.size >= FRAME:
            frame, self._pending = self._pending[:FRAME], self._pending[FRAME:]
            speech = self.vad(frame) >= self.th
            if self._speech or speech:
                self._speech.append(frame)
                self._since_partial += FRAME
                self._silent = 0 if speech else self._silent + 1
                total = len(self._speech) * FRAME
                if self._silent >= self.end_frames or total >= self.max_samples:
                    seg = np.concatenate(self._speech[: len(self._speech) - self._silent] or self._speech)
                    self._speech, self._silent, self._since_partial = [], 0, 0
                    if seg.size >= RATE * 0.4:
                        out.append(("final", seg))
                elif self._since_partial >= self.partial_every and total >= RATE:
                    self._since_partial = 0
                    out.append(("partial", np.concatenate(self._speech)))
        return out


def is_junk(text: str, seconds: float) -> bool:
    t = text.strip().lower().strip(".!?,")
    return not t or (t in _JUNK and seconds < 3) or set(t) <= set("♪.… ")


class Translator:
    """Other languages -> English with the local model (same settings as the chat model, so
    Ollama doesn't reload it)."""

    def __init__(self, settings, http=None):
        import httpx
        self.cfg = settings.brain.local
        self.http = http or httpx.AsyncClient(base_url=self.cfg.host, timeout=20)

    async def __call__(self, text: str, lang: str | None) -> str:
        body = {"model": self.cfg.model, "stream": False, "think": False, "keep_alive": self.cfg.keep_alive,
                "options": {"num_ctx": self.cfg.num_ctx, "temperature": 0.1},
                "messages": [{"role": "system", "content": "Translate the user's text into natural English. "
                              "Reply with only the translation, nothing else."},
                             {"role": "user", "content": text}]}
        r = await self.http.post("/api/chat", json=body)
        r.raise_for_status()
        return (r.json().get("message") or {}).get("content", "").strip()


class Subtitles:
    def __init__(self, transcribe: Callable[[np.ndarray], Awaitable[Any]], vad: Callable[[np.ndarray], float],
                 show: Callable[[str, str, bool], None], hide: Callable[[], None],
                 translate: Callable[[str, str | None], Awaitable[str]] | None = None,
                 capture: Any = None, nova_speaking: Callable[[], bool] = lambda: False,
                 on_caption: Callable[[dict], None] | None = None):
        self.transcribe, self.vad, self.show, self.hide = transcribe, vad, show, hide
        self.translate, self.capture = translate, capture or LoopbackCapture()
        self.nova_speaking, self.on_caption = nova_speaking, on_caption
        self.on = False
        self.translate_on = True
        self._queue: asyncio.Queue | None = None
        self._task: asyncio.Task | None = None
        self._last_shown = 0.0

    async def start(self, translate: bool = True) -> None:
        self.translate_on = translate
        if self.on:
            return
        loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue(maxsize=200)

        def on_audio(block: np.ndarray) -> None:
            loop.call_soon_threadsafe(self._put, block)
        await asyncio.to_thread(self.capture.start, on_audio)
        self.on = True
        self._task = asyncio.create_task(self._run())

    def _put(self, block: np.ndarray) -> None:
        if self._queue is not None and not self._queue.full():
            self._queue.put_nowait(block)

    async def stop(self) -> None:
        self.on = False
        self.capture.stop()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        self.hide()

    async def _run(self) -> None:
        seg = Segmenter(self.vad)
        while True:
            try:
                block = await asyncio.wait_for(self._queue.get(), 1.0)
            except asyncio.TimeoutError:
                block = None
            if self._last_shown and time.monotonic() - self._last_shown > 6:
                self.hide()                                   # nothing said for a while
                self._last_shown = 0.0
            if block is None:
                continue
            if self.nova_speaking():                          # Nova's own voice from the speakers
                seg = Segmenter(self.vad)
                continue
            for kind, audio in seg.feed(block):
                try:
                    await self._caption(kind, audio)
                except Exception as e:
                    log.warning("subtitles: %s", e)

    async def _caption(self, kind: str, audio: np.ndarray) -> None:
        t = await self.transcribe(audio)
        text, lang = (t.text, t.language) if hasattr(t, "text") else (str(t), None)
        if is_junk(text, audio.size / RATE):
            return
        english = ""
        if kind == "final" and self.translate_on and self.translate and lang and lang != "en":
            try:
                english = await self.translate(text, lang)
            except Exception as e:
                log.warning("subtitles: translation failed: %s", e)
        self.show(text, english, kind == "partial")
        self._last_shown = time.monotonic()
        if self.on_caption and kind == "final":
            self.on_caption({"type": "caption", "text": text, "english": english, "lang": lang})
