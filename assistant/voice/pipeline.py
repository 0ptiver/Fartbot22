"""The voice loop: mic -> VAD -> STT -> Claude -> sentence chunks -> TTS -> speakers.

Every stage streams into the next: STT is fed while you talk, TTS starts on the
first clause of the reply, and later sentences synthesize while earlier ones play.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
from typing import Any, Callable

import numpy as np

from assistant.brain.llm import Brain, BrainError, TextDelta, ToolFinished, ToolStarted, TurnComplete
from assistant.core.config import Settings
from assistant.core.conversation import Conversation
from assistant.tools.registry import ToolContext
from assistant.voice.chunker import SentenceChunker
from assistant.voice.latency import LatencyTracker
from assistant.voice.stt.base import STTProvider, STTSession
from assistant.voice.tts.base import TTSProvider
from assistant.voice.vad import Endpointer, SpeechEnd, SpeechPause, SpeechStart
from assistant.voice.textnorm import compact, normalize_words
from assistant.voice.wake import _norm, echo_overlap, match_wake

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
        self._tts_cache: dict[str, list[np.ndarray]] = {}
        self._follow_up_until = 0.0
        self._mute_until = 0.0
        self._last_said = ""
        self._named = False
        self.turns_done = 0
        # Phase 3: barge-in, merging, speculative STT
        self._handler: asyncio.Task | None = None     # resolving an utterance (STT + decisions)
        self._reply_started = False                   # current reply has started speaking
        self._spec: tuple[asyncio.Task, int] | None = None   # (early transcript, audio length)
        self._barge_check: asyncio.Task | None = None
        self._barge_checked = 0
        self._barged = False
        self._utt_kind = "normal"                     # how the current utterance started
        self._last_request = ("", 0.0)                # (text, time its speech ended)

    @property
    def busy(self) -> bool:
        """A reply is being worked on or spoken (or an utterance is being resolved)."""
        return any(t is not None and not t.done() for t in (self._turn, self._handler))

    @property
    def speaking(self) -> bool:
        return self.busy and self._reply_started

    # --- main loop ------------------------------------------------------------
    async def run(self, max_turns: int | None = None) -> None:
        async for frame, t in self.mic.frames():
            if self.cfg.mode == "ptt" and self.ptt is not None:
                await self._ptt_frame(frame, t)
            else:
                await self._open_mic_frame(frame, t)
            if max_turns is not None and self.turns_done >= max_turns and not self.busy:
                break
        for task in (self._handler, self._turn):
            if task:
                await asyncio.gather(task, return_exceptions=True)
        if self._turn:  # a handler may have started a reply
            await asyncio.gather(self._turn, return_exceptions=True)

    async def _open_mic_frame(self, frame: np.ndarray, t: float) -> None:
        bcfg = self.cfg.barge_in
        if (not self.busy and time.perf_counter() < self._mute_until) or (self.busy and bcfg.mode == "off"):
            # Ignore the echo tail right after speaking, or everything while busy if barge-in is off.
            if self.endpointer.in_speech or self._stt_session is not None:
                self.endpointer.reset()
                self._stt_session = None
            return
        prob = self.vad(frame)
        ev = self.endpointer.process(frame, prob, t)
        if isinstance(ev, SpeechStart):
            self._utt_kind = ("overlap" if self.speaking else "thinking" if self.busy else "normal")
            self._barged, self._barge_checked = False, 0
            self._cancel_spec()
            if self._utt_kind == "normal":
                self.on_event({"type": "listening"})
            self._stt_session = self.stt.session()
            for f in self.endpointer._buf:   # pre-roll + frames so far
                await self._stt_session.feed(f)
        elif isinstance(ev, SpeechPause):
            # Start transcribing during the pause; if it turns out to be the end, it's ready.
            if self._utt_kind != "overlap" and hasattr(self.stt, "transcribe"):
                self._cancel_spec()
                self._spec = (asyncio.create_task(self.stt.transcribe(ev.audio)), len(ev.audio))
        elif isinstance(ev, SpeechEnd):
            await self._utterance_done(ev)
        elif self.endpointer.in_speech and self._stt_session is not None:
            await self._stt_session.feed(frame)
            if self._utt_kind == "overlap" and not self._barged:
                await self._maybe_barge()

    def _cancel_spec(self) -> None:
        if self._spec is not None:
            self._spec[0].cancel()
            self._spec = None

    async def _maybe_barge(self) -> None:
        """You're talking while Nova speaks. Interrupt? (fast: on any speech; verified: only
        for words that aren't Nova's own voice coming back through the speakers)."""
        b = self.cfg.barge_in
        if b.mode == "fast":
            if self.endpointer.speech_seconds * 1000 >= b.fast_min_ms:
                self._barged = True
                await self.interrupt(reason="you started talking")
            return
        audio = self.endpointer.audio()
        due = len(audio) - self._barge_checked >= b.check_every_s * 16000
        if b.mode == "verified" and due and hasattr(self.stt, "transcribe") and (
                self._barge_check is None or self._barge_check.done()):
            self._barge_checked = len(audio)
            self._barge_check = asyncio.create_task(self._barge_verify(audio[-32000:]))

    async def _barge_verify(self, audio: np.ndarray) -> None:
        text = (await self.stt.transcribe(audio)).text
        if text and not self._barged and self.speaking and self._is_new_speech(text):
            self._barged = True
            await self.interrupt(reason=f'heard "{text}"')

    def _is_new_speech(self, text: str) -> bool:
        """Is this the user (not Nova's own voice)? Stop words, the name, or enough new words."""
        if self._stop_phrase(text) is not None or match_wake(text, self.cfg.wake.variants, 99)[0]:
            return True
        if len(normalize_words(text)) < self.cfg.barge_in.min_new_words:
            return False
        return not self._sounds_like_echo(text)

    def _sounds_like_echo(self, text: str) -> bool:
        """Is this transcript just Nova's own voice from the speakers? Compares normalized
        words ("90 x 90" == "ninety times ninety") as word pairs and as a letter sequence,
        so small mishearings ("fox" -> "fax") still count as echo."""
        from difflib import SequenceMatcher

        said, heard = normalize_words(self._last_said), normalize_words(text)
        if not heard:
            return True
        heard_c, said_c = compact(text), compact(self._last_said)
        if said_c and heard_c:
            m = SequenceMatcher(None, heard_c, said_c, autojunk=False)
            # Only runs of 4+ characters: scattered single letters match any long reply.
            matched = sum(b.size for b in m.get_matching_blocks() if b.size >= 4)
            if matched >= 0.7 * len(heard_c):
                return True
        if len(heard) < self.cfg.barge_in.min_new_words:
            # 1-2 words: echo if each appears in what was said ("8" from "8,100").
            return all(w in said or w in said_c for w in heard)
        said_pairs = set(zip(said, said[1:]))
        pairs = list(zip(heard, heard[1:]))
        return sum(p in said_pairs for p in pairs) >= 0.5 * len(pairs)

    def _stop_phrase(self, text: str) -> str | None:
        norm = " ".join(w for w in map(_norm, text.split()) if w)
        for phrase in sorted(self.cfg.barge_in.stop_words, key=len, reverse=True):
            p = " ".join(map(_norm, phrase.split()))
            # Only "stop" (+ filler) stops; "stop the music" is a request for Spotify.
            if norm == p or (norm.startswith(p + " ")
                             and set(norm[len(p):].split()) <= _STOP_FILLER):
                return phrase
        name_stripped = match_wake(text, self.cfg.wake.variants, 3)[1]
        if name_stripped and name_stripped != text:
            return self._stop_phrase(name_stripped)
        return None

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
        spec = None
        if self._spec is not None and end.silent_since_pause:
            spec = self._spec[0]      # transcribed during the final pause: nothing new since
        elif self._spec is not None:
            self._spec[0].cancel()
        self._spec = None
        # An utterance that already interrupted Nova stays an interruption even though
        # Nova is no longer busy by the time you finish speaking.
        kind = "overlap" if self._barged else (self._utt_kind if self.busy else "normal")
        if kind == "normal":
            self.on_event({"type": "thinking"})
        prev = self._handler if self._handler and not self._handler.done() else None
        self._handler = asyncio.create_task(
            self._handle_utterance(session, spec, lat, end.audio, kind, self._barged, prev))

    async def _handle_utterance(self, session, spec, lat: LatencyTracker, audio, kind: str,
                                barged: bool, prev: asyncio.Task | None) -> None:
        """Turn an utterance into text, then decide: new request, merge, interruption, or ignore."""
        started_reply = False
        try:
            if prev is not None:   # an earlier utterance is still being resolved
                await asyncio.gather(prev, return_exceptions=True)
            text = ""
            if spec is not None:
                try:
                    text = (await spec).text
                    lat.mark("stt_done")
                    lat.marks["stt_speculative"] = 1
                except asyncio.CancelledError:
                    if asyncio.current_task().cancelling():
                        raise
                    spec = None
            if spec is None:
                text = (await session.finish()).text if session else ""
                lat.mark("stt_done")
            if not text:
                if self.cfg.mode == "ptt":   # in hands-free modes this is just background noise
                    self.on_event({"type": "idle", "reason": "heard nothing",
                                   "hint": diagnose_silence(audio)})
                return

            if kind == "overlap":
                heard, text = text, self._strip_echo_prefix(text)
                if not text or (not barged and not self._is_new_speech(text)):
                    self.on_event({"type": "ignored", "text": heard, "reason": "sounded like my own voice"})
                    return
                await self.interrupt(reason=f'heard "{text}"')
                if self._stop_phrase(text) is not None:
                    self.on_event({"type": "stopped", "text": text})
                    self._named = False
                    self._mute_until = time.perf_counter() + self.cfg.wake.cooldown_ms / 1000
                    return
                rest = match_wake(text, self.cfg.wake.variants, self.cfg.wake.window_words)[1] or text
                self._named = True   # talking over Nova counts as talking to Nova
                text = rest
            elif kind == "thinking":
                prev_text, prev_end = self._last_request
                if self._stop_phrase(text) is not None:
                    await self.interrupt(reason=f'heard "{text}"')
                    self.on_event({"type": "stopped", "text": text})
                    return
                if prev_text and lat.marks["speech_end"] - prev_end <= self.cfg.barge_in.merge_window_s:
                    # You paused and carried on: one request, not two.
                    await self.interrupt(reason="you kept talking")
                    text = f"{prev_text} {text}"
                    self.on_event({"type": "merged", "text": text})
                elif self.cfg.mode == "wake":
                    checked = await self._check_wake(text, lat)
                    if checked is None:
                        return
                    await self.interrupt(reason="new request")
                    text = checked
                else:
                    await self.interrupt(reason="new request")
            elif self.cfg.mode == "wake":
                checked = await self._check_wake(text, lat)
                if checked is None:
                    return
                text = checked

            self._last_request = (text, lat.marks["speech_end"])
            self.on_event({"type": "transcript", "text": text})
            self._turn = asyncio.create_task(self._reply(text, lat))
            started_reply = True
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("voice turn failed")
            self.on_event({"type": "error", "message": f"{type(e).__name__}: {e}"})
        finally:
            if not started_reply:
                self.turns_done += 1

    async def _reply(self, text: str, lat: LatencyTracker) -> None:
        self._reply_started = False
        try:
            await self._speak_reply(text, lat)
            self._after_reply()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("voice reply failed")
            self.on_event({"type": "error", "message": f"{type(e).__name__}: {e}"})
        finally:
            self._reply_started = False
            self.turns_done += 1

    async def interrupt(self, reason: str = "") -> None:
        """Stop talking and cancel the reply in progress (barge-in, stop words, kill switch)."""
        self.player.stop()
        current = asyncio.current_task()
        cancelled = False
        for task in (self._turn, self._handler):
            if task is not None and not task.done() and task is not current:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                cancelled = True
        if cancelled:
            self.on_event({"type": "interrupted", "reason": reason})

    def _strip_echo_prefix(self, text: str) -> str:
        """Drop leading words that were Nova's own voice coming back through the speakers.
        Returns "" when everything was echo."""
        said = set(normalize_words(self._last_said))
        said_c = compact(self._last_said)
        words = text.split()
        i = 0
        while i < len(words) and all(w in said or w in said_c for w in normalize_words(words[i])):
            i += 1
        return " ".join(words[i:])

    async def _check_wake(self, text: str, lat: LatencyTracker) -> str | None:
        """Wake mode: only answer when addressed by name (or during the follow-up window)."""
        w = self.cfg.wake
        if self._last_said and echo_overlap(text, self._last_said) >= w.echo_overlap:
            self.on_event({"type": "ignored", "text": text, "reason": "sounded like my own voice"})
            return None
        addressed, rest = match_wake(text, w.variants, w.window_words)
        in_follow_up = time.perf_counter() < self._follow_up_until
        if not addressed and not in_follow_up:
            self.on_event({"type": "ignored", "text": text, "reason": "not addressed to me"})
            return None
        # Only a request that used the name opens a follow-up window; follow-ups don't
        # chain, so a conversation with someone else in the room isn't answered.
        self._named = addressed
        if not addressed:
            self._follow_up_until = 0.0
        if addressed and not rest:              # just "Nova" -> "Yes, sir?"
            self.on_event({"type": "transcript", "text": text})
            await self.say(w.acknowledgement)
            self._after_reply()
            return None
        return rest if addressed else text

    def _after_reply(self) -> None:
        now = time.perf_counter()
        self._mute_until = now + self.cfg.wake.cooldown_ms / 1000
        if self.cfg.mode == "wake" and self._named and self.cfg.wake.follow_up_s > 0:
            self._follow_up_until = now + self.cfg.wake.follow_up_s
            self.on_event({"type": "follow_up", "seconds": self.cfg.wake.follow_up_s})

    async def say(self, text: str) -> None:
        """Speak a fixed line (acknowledgements, notices)."""
        self._last_said = text
        self._reply_started = True
        self.on_event({"type": "speak", "text": text})
        await self._synth_and_play(text, None)
        await self.player.drain()

    async def prewarm(self) -> None:
        """Pre-synthesize short fixed phrases so they play instantly."""
        for phrase in [*self.cfg.filler_phrases, self.cfg.wake.acknowledgement]:
            if phrase and phrase not in self._tts_cache:
                self._tts_cache[phrase] = [a async for a in self.tts.synthesize(phrase)]

    async def _synth_and_play(self, chunk: str, lat: LatencyTracker | None) -> None:
        cached = self._tts_cache.get(chunk)
        if cached is None:
            cached = []
            async for audio in self.tts.synthesize(chunk):
                if lat:
                    lat.mark("tts_first_audio")
                self.player.play(audio)
                cached.append(audio)
            if len(chunk) <= 40 and len(self._tts_cache) < 200:
                self._tts_cache[chunk] = cached
            return
        for audio in cached:
            if lat:
                lat.mark("tts_first_audio")
            self.player.play(audio)

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
                    lat.mark("llm_first_token")  # a tool call is the model's first response too
                    push(chunker.flush())       # speak what's been said so far first
                    if chunker.emitted == 0 and ev.name in self.cfg.filler_tools:
                        filler = next(self._fillers)
                        if filler:
                            push([filler])
                            chunker.emitted += 1
                    self.on_event({"type": "tool", "name": ev.name})
                elif isinstance(ev, ToolFinished):
                    self.on_event({"type": "tool_done", "name": ev.name, "ms": ev.duration_ms,
                                   "is_error": ev.is_error, "summary": ev.summary})
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
        said = []
        while (chunk := await queue.get()) is not None:
            self._reply_started = True
            said.append(chunk)
            self._last_said = " ".join(said)
            await self._synth_and_play(chunk, lat)


_STOP_FILLER = {"please", "now", "thanks", "thank", "you", "nova", "sir", "for", "a", "sec",
                "second", "moment", "right", "there", "it", "talking"}


def diagnose_silence(audio: np.ndarray | None) -> str:
    """Explain an empty transcript in plain words."""
    if audio is None or audio.size == 0:
        return "no audio was recorded"
    seconds = audio.size / 16000
    peak = float(np.max(np.abs(audio)))
    if seconds < 0.4:
        return f"you only held the keys for {seconds:.1f}s. Hold them the whole time you talk"
    if peak < 0.01:
        return (f"the mic is silent (level {peak:.1%}). Wrong mic, muted, or blocked in Windows "
                "privacy settings? Try: python -m assistant mictest")
    return f"recorded {seconds:.1f}s at level {peak:.0%} but couldn't make out words. Speak a bit closer?"
