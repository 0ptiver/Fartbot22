"""Talking to Nova from the phone, like at the desk. Owner: "I want to be able to speak to Nova as if
I was sitting here at my computer, does the same things, answers the same stuff".

The phone records a voice message (its own mic, in the browser) and sends it to the PC. The PC
does exactly what it does with the headset: Whisper turns it into words, the same brain
answers, and Kokoro says the reply in Nova's own voice, which goes back to the phone and plays.
Nothing leaves the tailnet; no audio is kept.
"""

from __future__ import annotations

import asyncio
import base64
import io
import re
import wave

import numpy as np

MAX_BYTES = 8_000_000               # ~1 minute of phone audio is well under this
MIN_SAMPLES = 16000 // 4            # under 0.25 s: nothing was said


def decode(data: bytes) -> np.ndarray:
    """Whatever the phone recorded (webm/opus on Android, mp4/aac on iPhone) -> 16 kHz mono float32."""
    from faster_whisper.audio import decode_audio
    return decode_audio(io.BytesIO(data), sampling_rate=16000)


async def transcribe(loop, data: bytes) -> str:
    """Words from a voice message, with the same Whisper the headset uses."""
    stt = getattr(loop, "stt", None)
    if stt is None:
        raise RuntimeError("Nova's hearing isn't running on the PC.")
    audio = await asyncio.to_thread(decode, data)
    if audio.size < MIN_SAMPLES:
        return ""
    session = stt.session()
    await session.feed(audio)
    return ((await session.finish()).text or "").strip()


def spoken(text: str, settings) -> str:
    """What to say out loud: the same clean-up and length as at the desk."""
    from assistant.voice.speechtext import clean_for_speech
    t = clean_for_speech(text or "").strip()
    cap = int(getattr(settings.voice, "max_spoken_sentences", 3) or 3)
    parts = re.split(r"(?<=[.!?])\s+", t)
    return " ".join(parts[:cap]).strip()


async def speak(loop, settings, text: str) -> str | None:
    """Nova's voice saying `text`, as a base64 WAV (None if there's nothing to say or no voice)."""
    tts = getattr(loop, "tts", None)
    say = spoken(text, settings)
    if tts is None or not say:
        return None
    chunks = [c async for c in tts.synthesize(say)]
    if not chunks:
        return None
    audio = np.concatenate([np.asarray(c, dtype=np.float32).reshape(-1) for c in chunks])
    pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype("<i2").tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(getattr(tts, "sample_rate", 24000)))
        w.writeframes(pcm)
    return base64.b64encode(buf.getvalue()).decode("ascii")
