"""The voice loop: mic -> VAD -> STT -> Claude -> sentence chunks -> TTS -> speakers.

Every stage streams into the next: STT is fed while you talk, TTS starts on the
first clause of the reply, and later sentences synthesize while earlier ones play.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
from typing import Any, Callable

import numpy as np

from assistant.brain.llm import Brain, BrainError, TextDelta, ToolStarted, TurnComplete
from assistant.core.config import Settings
from assistant.core.conversation import Conversation
from assistant.tools.registry import ToolContext
from assistant.voice.chunker import SentenceChunker
from assistant.voice.latency import LatencyTracker
from assistant.voice.stt.base import STTProvider, STTSession
from assistant.voice.tts.base import TTSProvider
from assistant.voice.vad import Endpointer, SpeechEnd, SpeechStart

log = logging.getLogger(__name__)

OnEvent = Callable[[dict[str, Any]], None]


class VoiceLoop:
    def __init__(self, settings: Settings, brain: Brain, stt: STTProvider, tts: TTSProvider,
                 mic, player, vad, ptt=None, on_event: OnEvent | None = None):
        self.settings = settings
        self.cfg = settings.voice
        self.brain = brain
        self.stt = stt
        self.tts = tts
        self.mic = mic
        self.player = player
        self.vad = vad                      # callable: frame -> P(speech)
        self.ptt = ptt
        self.on_event = on_event or (lambda ev: None)
        self.endpointer = Endpointer(self.cfg.vad)
        self.conv = Conversation(settings.brain.history_turns)
        self.ctx = ToolContext(settings, client_id="voice", services={"brain": brain})
        self._stt_session: STTSession | None = None
        self._turn: asyncio.Task | None = None
        self._ptt_was_held = False
        self._ptt_frames: list[np.ndarray] = []
        self._ptt_t_last = 0.0
        self.last_latency: LatencyTracker | None = None
        self._fillers = itertools.cycle(self.cfg.filler_phrases or [""])
        self.turns_done = 0

    @property
    def busy(self) -> bool:
        return self._turn is not None and not self._turn.done()

    # --- main loop ------------------------------------------------------------
    async def run(self, max_turns: int | None = None) -> None:
        async for frame, t in self.mic.frames():
            if self.cfg.mode == "ptt" and self.ptt is not None:
                await self._ptt_frame(frame, t)
            else:
                await self._open_mic_frame(frame, t)
            if max_turns is not None and self.turns_done >= max_turns and not self.busy:
                break
        if self._turn:
            await asyncio.gather(self._turn, return_exceptions=True)

    async def _open_mic_frame(self, frame: np.ndarray, t: float) -> None:
        if self.busy:
            # Half-duplex until barge-in + echo cancellation arrive in Phase 3:
            # don't listen to ourselves while speaking.
            return
        prob = self.vad(frame)
        ev = self.endpointer.process(frame, prob, t)
        if isinstance(ev, SpeechStart):
            self.on_event({"type": "listening"})
            self._stt_session = self.stt.session()
            for f in self.endpointer._buf:   # pre-roll + frames so far
                await self._stt_session.feed(f)
        elif isinstance(ev, SpeechEnd):
            await self._utterance_done(ev)
        elif self.endpointer.in_speech and self._stt_session is not None:
            await self._stt_session.feed(frame)

    async def _ptt_frame(self, frame: np.ndarray, t: float) -> None:
        held = self.ptt.held.is_set()
        if held and not self._ptt_was_held:
            # Pressing the key while it's talking interrupts it (barge-in).
            await self.interrupt()
            self.on_event({"type": "listening"})
            self._stt_session = self.stt.session()
            self._ptt_frames = []
        if held:
            self._ptt_frames.append(frame)
            if self.vad(frame) >= self.cfg.vad.threshold:
                self._ptt_t_last = t
            await self._stt_session.feed(frame)
        elif self._ptt_was_held:
            audio = np.concatenate(self._ptt_frames) if self._ptt_frames else np.zeros(0, np.float32)
            await self._utterance_done(SpeechEnd(audio, t, t))
        self._ptt_was_held = held

    async def _utterance_done(self, end: SpeechEnd) -> None:
        lat = LatencyTracker()
        lat.mark("speech_end", end.t_last_speech)
        lat.mark("eot_detected", end.t_detected)
        session, self._stt_session = self._stt_session, None
        self.on_event({"type": "thinking"})
        self._turn = asyncio.create_task(self._respond(session, lat))

    async def interrupt(self) -> None:
        self.player.stop()
        if self.busy:
            self._turn.cancel()
            await asyncio.gather(self._turn, return_exceptions=True)
            self.on_event({"type": "interrupted"})

    # --- one spoken turn ------------------------------------------------------
    async def _respond(self, session: STTSession | None, lat: LatencyTracker) -> None:
        try:
            text = (await session.finish()).text if session else ""
            lat.mark("stt_done")
            if not text:
                self.on_event({"type": "idle", "reason": "heard nothing"})
                return
            self.on_event({"type": "transcript", "text": text})
            await self._speak_reply(text, lat)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("voice turn failed")
            self.on_event({"type": "error", "message": f"{type(e).__name__}: {e}"})
        finally:
            self.turns_done += 1

    async def _speak_reply(self, text: str, lat: LatencyTracker) -> None:
        chunker = SentenceChunker(self.cfg.first_chunk_min_chars,
                                  honorific=self.settings.assistant.address_user_as)
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        self.player.reset_marker()
        speaker = asyncio.create_task(self._speaker(queue, lat))

        def push(chunks: list[str]) -> None:
            for c in chunks:
                lat.mark("first_chunk")
                self.on_event({"type": "speak", "text": c})
                queue.put_nowait(c)

        try:
            async for ev in self.brain.run_turn(self.conv, text, self.ctx):
                if isinstance(ev, TextDelta):
                    lat.mark("llm_first_token")
                    self.on_event({"type": "text", "text": ev.text})
                    push(chunker.feed(ev.text))
                elif isinstance(ev, ToolStarted):
                    push(chunker.flush())       # speak what's been said so far first
                    if chunker.emitted == 0 and ev.name in self.cfg.filler_tools:
                        filler = next(self._fillers)
                        if filler:
                            push([filler])
                            chunker.emitted += 1
                    self.on_event({"type": "tool", "name": ev.name})
                elif isinstance(ev, BrainError):
                    push(chunker.flush())
                    push([ev.message])
                    self.on_event({"type": "error", "message": ev.message})
                elif isinstance(ev, TurnComplete):
                    push(chunker.flush())
                    self.on_event({"type": "turn_complete", "timings": ev.timings, "usage": ev.usage})
            queue.put_nowait(None)
            await speaker
            await self.player.drain()
        finally:
            if not speaker.done():
                speaker.cancel()
                await asyncio.gather(speaker, return_exceptions=True)

        if self.player.first_audio_at:
            lat.mark("playback_start", self.player.first_audio_at)
        self.last_latency = lat
        self.on_event({"type": "latency", "breakdown": lat.breakdown(),
                       "report": lat.report()})
        self.on_event({"type": "idle"})

    async def _speaker(self, queue: asyncio.Queue, lat: LatencyTracker) -> None:
        while (chunk := await queue.get()) is not None:
            async for audio in self.tts.synthesize(chunk):
                lat.mark("tts_first_audio")
                self.player.play(audio)
