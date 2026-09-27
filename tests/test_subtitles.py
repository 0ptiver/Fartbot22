"""Live subtitles: resampling, segmenting (real Silero VAD), junk filter, translation, the pipeline."""

import asyncio
import json

import httpx
import numpy as np
import pytest

from assistant.tools.registry import ToolContext
from assistant.voice import subtitles as SUB
from assistant.voice.stt.base import Transcript
from tests.voice_helpers import WAV, silero_or_skip


def loud(v):
    return lambda frame: 0.9 if np.max(np.abs(frame)) > 0.05 else 0.0


def test_resample_48k_stereo_to_16k_mono():
    t = np.arange(4800) / 48000
    stereo = np.stack([np.sin(2 * np.pi * 440 * t), np.sin(2 * np.pi * 440 * t)], axis=1).astype(np.float32)
    mono = SUB.to_16k_mono(stereo, 48000)
    assert mono.shape == (1600,) and mono.dtype == np.float32 and 0.9 < np.max(np.abs(mono)) <= 1.0


def test_segmenter_finals_and_partials():
    seg = SUB.Segmenter(loud(None), partial_every_s=1.0)
    speech, silence = np.full(16000, 0.3, np.float32), np.zeros(16000, np.float32)
    events = seg.feed(np.concatenate([speech, speech, silence]))
    kinds = [k for k, _ in events]
    assert "partial" in kinds and kinds[-1] == "final"
    final = events[-1][1]
    assert 1.9 < final.size / 16000 < 2.2                         # the speech, not the trailing silence
    assert seg.feed(np.zeros(8000, np.float32)) == []


def test_segmenter_with_real_silero():
    import soundfile as sf
    vad = silero_or_skip()
    audio, sr = sf.read(WAV, dtype="float32")
    audio = SUB.to_16k_mono(audio, sr) if sr != 16000 else audio
    events = SUB.Segmenter(vad).feed(np.concatenate([np.zeros(8000, np.float32), audio, np.zeros(16000, np.float32)]))
    assert [k for k, _ in events][-1] == "final"


def test_junk_filter():
    assert SUB.is_junk("Thank you.", 1.0) and SUB.is_junk("♪ ♪", 5) and SUB.is_junk("", 1)
    assert not SUB.is_junk("Thank you.", 4.0) and not SUB.is_junk("¿Dónde está el coche?", 1.0)


async def test_translator_keeps_the_chat_models_settings(settings):
    seen = []

    def handler(req):
        seen.append(json.loads(req.content))
        return httpx.Response(200, json={"message": {"content": "Where is the car?"}})
    tr = SUB.Translator(settings, http=httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://o"))
    assert await tr("¿Dónde está el coche?", "es") == "Where is the car?"
    body = seen[0]
    assert body["model"] == settings.brain.local.model
    assert body["options"]["num_ctx"] == settings.brain.local.num_ctx   # a different num_ctx reloads the model


class FakeCapture:
    def __init__(self):
        self.on_audio, self.stopped = None, False

    def start(self, on_audio):
        self.on_audio = on_audio

    def stop(self):
        self.stopped = True


async def test_pipeline_captions_translates_and_skips_nova():
    shown, hidden, events = [], [], []
    speaking = [False]

    async def transcribe(audio):
        return Transcript("¿Dónde está el coche?", "es")

    async def translate(text, lang):
        return "Where is the car?"
    cap = FakeCapture()
    subs = SUB.Subtitles(transcribe, loud(None), lambda t, e, p: shown.append((t, e, p)), lambda: hidden.append(1),
                         translate=translate, capture=cap, nova_speaking=lambda: speaking[0],
                         on_caption=events.append)
    await subs.start()
    speech, silence = np.full(1600, 0.3, np.float32), np.zeros(1600, np.float32)
    for _ in range(15):
        cap.on_audio(speech)
    for _ in range(10):
        cap.on_audio(silence)
    for _ in range(50):
        if events:
            break
        await asyncio.sleep(0.02)
    assert shown[-1] == ("¿Dónde está el coche?", "Where is the car?", False)
    assert events[0]["english"] == "Where is the car?"
    n = len(shown)
    speaking[0] = True                                               # Nova talking: not captioned
    for _ in range(15):
        cap.on_audio(speech)
    for _ in range(10):
        cap.on_audio(silence)
    await asyncio.sleep(0.2)
    assert len(shown) == n
    await subs.stop()
    assert cap.stopped and hidden


async def test_tool(settings, registry):
    started = []

    class Subs:
        on = False

        async def start(self, translate=True):
            self.on = True
            started.append(translate)

        async def stop(self):
            self.on = False
    ctx = ToolContext(settings, services={"subtitles": Subs()})
    res = await registry.execute("subtitles", {"on": True}, ctx)
    assert "translating into English" in res.content and started == [True]
    assert (await registry.execute("subtitles", {"on": False}, ctx)).content == "Subtitles off."
    assert (await registry.execute("subtitles", {"on": True}, ToolContext(settings))).is_error


@pytest.mark.parametrize("text,expected", [
    ("turn on subtitles", {"on": True}), ("translate this", {"on": True, "translate": True}),
    ("what are they saying", {"on": True, "translate": True}), ("subtitles off", {"on": False}),
    ("stop translating", {"on": False}),
])
def test_phrases(text, expected):
    from assistant.brain.intents import match_intent
    assert match_intent(text) == ("subtitles", expected)
