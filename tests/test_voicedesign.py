"""Voice designer: blending, pitch, saving, live switching, the HUD tab and 'change your voice'."""

import numpy as np
import pytest

from assistant.brain.intents import match_intent
from assistant.tools.registry import ToolContext
from assistant.voice import voicedesign as VD
from assistant.voice.tts.kokoro import KokoroTTS


def test_clean_limits_everything():
    d = VD.VoiceDesign({"bm_george": 3, "bm_lewis": 1, "bf_emma": 1, "af_nova": 1, "../evil": 5, "bm_x": 0},
                       speed=9, pitch=-20, lang="klingon").clean()
    assert len(d.mix) == 3 and abs(sum(d.mix.values()) - 1) < 0.01 and d.mix["bm_george"] == 0.6
    assert "../evil" not in d.mix and (d.speed, d.pitch, d.lang) == (1.4, -4.0, "en-gb")
    assert VD.VoiceDesign({"zz_nope": 1}).clean(["bm_george"]).mix == {"bm_george": 1.0}


def test_blend_and_catalog():
    styles = {"a": np.ones((4, 1, 3), np.float32), "b": np.zeros((4, 1, 3), np.float32)}
    mixed = VD.blend(styles.__getitem__, {"a": 0.25, "b": 0.75})
    assert mixed.shape == (4, 1, 3) and np.allclose(mixed, 0.25)
    assert VD.blend(styles.__getitem__, {"a": 1.0}) == "a"                  # one voice: by name
    cat = VD.catalog(["bm_george", "af_nova", "jf_alpha", "zf_xiaobei"])
    assert cat == [{"id": "af_nova", "label": "Nova (American woman)"},
                   {"id": "bm_george", "label": "George (British man)"}]      # English voices only


def test_repitch_moves_the_pitch():
    sr = 24000
    t = np.arange(sr) / sr
    tone = np.sin(2 * np.pi * 200 * t).astype(np.float32)
    up = VD.repitch(tone, 12)                                                # an octave up
    freqs = np.fft.rfftfreq(up.size, 1 / sr)
    assert abs(freqs[np.argmax(np.abs(np.fft.rfft(up)))] - 400) < 5
    assert up.size == sr // 2                    # shorter; the TTS is asked to talk slower to match
    assert VD.repitch(tone, 0) is tone


def test_save_and_load(tmp_path):
    d = VD.VoiceDesign({"bm_george": 0.6, "bm_lewis": 0.4}, 1.1, -1.5, "en-gb")
    d.save()
    assert VD.VoiceDesign.load(VD.VoiceDesign()) == d
    (tmp_path / "voice.json").write_text("not json")
    assert VD.VoiceDesign.load(VD.VoiceDesign({"bf_emma": 1.0})).mix == {"bf_emma": 1.0}   # broken file: fallback


class FakeKokoro:
    def __init__(self):
        self.calls = []

    def get_voices(self):
        return ["bm_george", "bm_lewis", "bf_emma"]

    def get_voice_style(self, name):
        return np.full((4, 1, 3), len(name), np.float32)

    def create(self, text, voice, speed, lang, trim=True):
        self.calls.append((voice if isinstance(voice, str) else "blend", round(speed, 3), lang))
        return np.zeros(24000, np.float32), 24000


async def test_kokoro_uses_the_design(settings):
    tts = KokoroTTS(settings.voice.tts.kokoro)
    tts.kokoro = FakeKokoro()
    tts._apply(VD.VoiceDesign({"bm_george": 0.5, "bm_lewis": 0.5}, 1.0, 4.0, "en-us"))
    out = [a async for a in tts.synthesize("Hello")]
    assert tts.kokoro.calls[-1] == ("blend", 0.794, "en-us")               # slower, then 4 semitones up
    assert out[0].size == round(24000 / 2 ** (4 / 12))                      # same talking speed overall
    audio = await tts.preview(VD.VoiceDesign({"bf_emma": 1.0}), "Hi")
    assert tts.kokoro.calls[-1] == ("bf_emma", 1.05, "en-gb") and audio.size == 24000
    assert tts.design.mix == {"bm_george": 0.5, "bm_lewis": 0.5}           # preview didn't switch


async def test_voice_loop_switch_saves_and_clears_cache(settings, registry, tmp_path):
    from tests.test_barge import make
    from tests.voice_helpers import FakeTTS

    class DesignTTS(FakeTTS):
        def __init__(self):
            super().__init__()
            self.design = VD.VoiceDesign()

        async def set_design(self, d):
            self.design = d.clean()

        async def preview(self, d, text):
            return np.zeros(10, np.float32)

    loop, _ = make(settings, registry, [], [("quiet", 1)], tts=DesignTTS())
    loop._tts_cache["Yes, sir?"] = [np.zeros(3, np.float32)]
    await loop.set_voice(VD.VoiceDesign({"bf_emma": 1.0}))
    assert VD.VoiceDesign.load(VD.VoiceDesign()).mix == {"bf_emma": 1.0}
    assert "Yes, sir?" in loop.tts.spoken                                    # re-made in the new voice
    res = await registry.execute("set_voice", {"preset": "deep butler"}, ToolContext(settings, services={"voice": loop}))
    assert res.content == "Voice changed to Deep butler. How's this?" and loop.tts.design.pitch == -1.5
    bad = await registry.execute("set_voice", {"preset": "squirrel"}, ToolContext(settings, services={"voice": loop}))
    assert bad.is_error and "Butler" in bad.content


@pytest.mark.parametrize("text,expected", [
    ("change your voice to deep butler", ("set_voice", {"preset": "deep butler"})),
    ("use the movie trailer voice", ("set_voice", {"preset": "movie trailer"})),
    ("use your normal voice", ("set_voice", {"preset": "normal"})),
])
def test_phrases(text, expected):
    assert match_intent(text) == expected


def test_hud_voice_tab(settings):
    from fastapi.testclient import TestClient

    from assistant.hud import server as hud
    from tests.test_hud import KEY, FakeLoop, HOST, connect, recv

    class Loop(FakeLoop):
        def __init__(self):
            super().__init__()
            self.tts = type("T", (), {"voices": lambda s: ["bm_george", "bf_emma"], "design": VD.VoiceDesign()})()
            self.previews = []

        async def preview_voice(self, d, text):
            self.previews.append((d.mix, text))

        async def set_voice(self, d):
            self.tts.design = d

    loop = Loop()
    client = TestClient(hud.create_hud_app(settings, loop, hud.Hub(), KEY, trust_test_client=True))
    assert client.get("/voice.js", headers=HOST).status_code == 200
    ws = connect(client)
    ws.send_json({"type": "voice_get"})
    info = recv(ws, "voice")
    assert [v["id"] for v in info["voices"]] == ["bf_emma", "bm_george"] and "Butler" in info["presets"]
    ws.send_json({"type": "voice_preview", "design": {"mix": {"bf_emma": 1}}, "text": ""})
    ws.send_json({"type": "voice_save", "design": {"mix": {"bf_emma": 1}, "pitch": 2}})
    assert recv(ws, "toast")["text"].startswith("Saved")
    ws.close()
    assert loop.previews == [({"bf_emma": 1.0}, hud.PREVIEW_LINE)] and loop.tts.design.pitch == 2.0
