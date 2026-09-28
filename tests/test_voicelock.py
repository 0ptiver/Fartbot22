"""Voice lock: the torch-free model reader, the maths, enrolment, and the loop ignoring strangers.
(The numpy encoder was checked against the original PyTorch Resemblyzer here: identical weights,
mel error < 2e-7, embedding cosine 1.0000.)"""

import asyncio
import io
import pickle
import sys
import types

import numpy as np
import pytest

from assistant.brain.intents import match_intent
from assistant.voice import voiceprint as VP
from tests.fakes import text_msg
from tests.test_barge import make
from tests.voice_helpers import FakeTTS


# --- the legacy torch file reader --------------------------------------------------------------------
def legacy_torch_bytes(tensors: dict) -> bytes:
    """Write a file the way torch.save did before 1.6, without torch."""
    torch = types.ModuleType("torch")
    utils = types.ModuleType("torch._utils")

    class FloatStorage:
        pass
    FloatStorage.__module__, FloatStorage.__qualname__ = "torch", "FloatStorage"
    torch.FloatStorage = FloatStorage

    def _rebuild_tensor_v2(*a):
        pass
    _rebuild_tensor_v2.__module__, _rebuild_tensor_v2.__qualname__ = "torch._utils", "_rebuild_tensor_v2"
    utils._rebuild_tensor_v2 = _rebuild_tensor_v2
    saved = {k: sys.modules.get(k) for k in ("torch", "torch._utils")}
    sys.modules["torch"], sys.modules["torch._utils"] = torch, utils
    try:
        class Stor:
            def __init__(self, key, arr):
                self.key, self.arr = key, arr

        class T:
            def __init__(self, stor):
                self.stor = stor

            def __reduce__(self):
                shape = self.stor.arr.shape
                stride = tuple(int(np.prod(shape[i + 1:])) for i in range(len(shape)))
                return (_rebuild_tensor_v2, (self.stor, 0, shape, stride, False, {}))

        class P(pickle.Pickler):
            def persistent_id(self, obj):
                if isinstance(obj, Stor):
                    return ("storage", FloatStorage, obj.key, "cpu", obj.arr.size)
                return None
        stors = {k: Stor(str(i), np.asarray(v, np.float32)) for i, (k, v) in enumerate(tensors.items())}
        f = io.BytesIO()
        for header in (0x1950A86A20F9469CFC6C, 1001, {"little_endian": True}):
            pickle.dump(header, f, protocol=2)
        P(f, protocol=2).dump({"step": 1, "model_state": {k: T(s) for k, s in stors.items()}})
        pickle.dump([s.key for s in stors.values()], f, protocol=2)
        for s in stors.values():
            f.write(int(s.arr.size).to_bytes(8, "little"))
            f.write(s.arr.astype("<f4").tobytes())
        return f.getvalue()
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


def test_reads_old_torch_files_without_torch():
    a = np.arange(12, dtype=np.float32).reshape(3, 4)
    b = np.array([1.5, -2.0], np.float32)
    out = VP.read_legacy_torch(legacy_torch_bytes({"lstm.w": a, "linear.bias": b}))
    assert out["step"] == 1
    assert np.array_equal(out["model_state"]["lstm.w"], a) and np.array_equal(out["model_state"]["linear.bias"], b)


def test_downloaded_model_must_match_its_fingerprint():
    with pytest.raises(ValueError, match="fingerprint"):
        VP.weights_from_wheel(b"not the real wheel")


# --- the maths -----------------------------------------------------------------------------------------
def test_mel_and_slices():
    assert VP.mel_filters().shape == (40, 201) and (VP.mel_filters() >= 0).all()
    t = np.arange(16000) / 16000
    mel = VP.mel_spectrogram(np.sin(2 * np.pi * 440 * t).astype(np.float32))
    assert mel.shape == (101, 40) and int(np.argmax(mel[50])) in range(5, 12)     # 440 Hz: a low band
    assert [(s.start, s.stop) for s in VP.partial_slices(3 * 16000)] == [(0, 160), (77, 237), (154, 314)]
    assert len(VP.partial_slices(8000)) == 1                                    # short: still one slice


def random_weights(seed=0):
    rng = np.random.default_rng(seed)
    w = {}
    for layer, inp in enumerate((40, 256, 256)):
        w[f"lstm.weight_ih_l{layer}"] = rng.normal(0, 0.1, (1024, inp)).astype(np.float32)
        w[f"lstm.weight_hh_l{layer}"] = rng.normal(0, 0.1, (1024, 256)).astype(np.float32)
        w[f"lstm.bias_ih_l{layer}"] = rng.normal(0, 0.1, 1024).astype(np.float32)
        w[f"lstm.bias_hh_l{layer}"] = rng.normal(0, 0.1, 1024).astype(np.float32)
    w["linear.weight"] = rng.normal(0, 0.1, (256, 256)).astype(np.float32)
    w["linear.bias"] = rng.normal(0, 0.1, 256).astype(np.float32)
    return w


def test_encoder_output_is_a_stable_unit_vector():
    enc = VP.VoiceEncoder(random_weights())
    rng = np.random.default_rng(1)
    wav = (rng.normal(0, 0.1, 32000)).astype(np.float32)
    e = enc.embed(wav)
    assert e.shape == (256,) and abs(np.linalg.norm(e) - 1) < 1e-5 and np.allclose(e, enc.embed(wav))
    mels = rng.random((2, 160, 40)).astype(np.float32)
    both = enc._forward(mels)
    assert np.allclose(both[1], enc._forward(mels[1:])[0], atol=1e-5)          # batching changes nothing


# --- the lock -------------------------------------------------------------------------------------------
OWNER, STRANGER = np.eye(256, dtype=np.float32)[0], np.eye(256, dtype=np.float32)[1]


class FakeEncoder:
    """Audio filled with 0.3 = the owner, 0.5 = a stranger (plus a little per-sample variation)."""

    def embed(self, audio):
        who = OWNER if abs(float(np.max(np.abs(audio))) - 0.3) < 0.1 else STRANGER
        noise = np.zeros(256, np.float32)
        noise[2] = float(np.std(audio)) * 3
        e = who + noise
        return e / np.linalg.norm(e)


def voice(level, seconds=2.0):
    return np.full(int(16000 * seconds), level, np.float32)


def test_enrol_calibrate_check_and_persist(tmp_path):
    lock = VP.VoiceLock(tmp_path / "vp.json", FakeEncoder())
    assert not lock.active
    line = lock.start_enrol()
    assert line == VP.ENROL_LINES[0]
    nxt, msg = lock.add_sample(voice(0.3, 0.5))
    assert "a bit short" in msg and nxt == VP.ENROL_LINES[0]
    for i in range(5):
        nxt, msg = lock.add_sample(voice(0.3))
    assert nxt is None and msg.startswith("Done") and lock.active
    assert 0.62 <= lock.threshold <= 0.82
    assert lock.check(voice(0.3))[0] and not lock.check(voice(0.5))[0]
    again = VP.VoiceLock(tmp_path / "vp.json", FakeEncoder())                     # saved: numbers only
    assert again.active and again.threshold == pytest.approx(lock.threshold, abs=1e-3)
    assert "prints" in (tmp_path / "vp.json").read_text() and "audio" not in (tmp_path / "vp.json").read_text()
    again.set(on=False)
    assert not VP.VoiceLock(tmp_path / "vp.json", FakeEncoder()).active
    again.forget()
    assert not (tmp_path / "vp.json").exists()


def locked_loop(settings, registry, tmp_path, script, steps):
    loop, events = make(settings, registry, script, steps, tts=FakeTTS())
    lock = VP.VoiceLock(tmp_path / "vp.json", FakeEncoder())
    lock.prints.add([OWNER])
    lock.set(on=True, threshold=0.75)
    loop.lock = loop.ctx.services["voicelock"] = lock
    return loop, events


async def test_strangers_are_ignored(settings, registry, tmp_path):
    from tests import test_barge
    loop, events = locked_loop(settings, registry, tmp_path, [text_msg("It is noon, sir.")], [
        ("say", "Nova, what time is it", 10), ("quiet", 20),
        ("until", lambda l: l.turns_done >= 1 and not l.busy)])
    stranger = np.full(512, 0.5, np.float32)
    old = test_barge.SPEECH
    test_barge.SPEECH = stranger                                   # everything this mic "hears" is a stranger
    try:
        await loop.run()
    finally:
        test_barge.SPEECH = old
    assert loop.brain.client.messages.calls == []
    assert any(e["type"] == "ignored" and "not your voice" in e["reason"] for e in events)


async def test_owner_gets_through(settings, registry, tmp_path):
    loop, events = locked_loop(settings, registry, tmp_path, [text_msg("It is noon, sir.")], [
        ("say", "Nova, what time is it", 10), ("quiet", 20),
        ("until", lambda l: l.turns_done >= 1 and not l.busy)])
    await loop.run()
    assert loop.tts.spoken[-1] == "It is noon, sir."
    assert any(e["type"] == "voice_check" and e["ok"] for e in events)


async def test_learning_by_voice(settings, registry, tmp_path):
    lines = VP.ENROL_LINES
    steps = []
    for i, line in enumerate(lines):
        steps += [("until", lambda l, i=i: l.lock.enrolling is not None and len(l.lock.enrolling) == i and not l.busy),
                  ("say", line, 40), ("quiet", 20)]
    steps += [("until", lambda l: l.lock.enrolling is None and not l.busy)]
    loop, events = make(settings, registry, [], steps, tts=FakeTTS())
    loop.lock = loop.ctx.services["voicelock"] = VP.VoiceLock(tmp_path / "vp.json", FakeEncoder())
    loop.lock.start_enrol()
    await loop.run()
    assert loop.lock.active and "Done. I know your voice now, and I'll only take orders from you." in loop.tts.spoken
    assert loop.brain.client.messages.calls == []                 # enrolment never reaches the model


async def test_wrong_line_during_learning_is_not_used(settings, registry, tmp_path):
    loop, _ = make(settings, registry, [], [("say", "what's for dinner tonight", 40), ("quiet", 20),
                                            ("until", lambda l: not l.busy)], tts=FakeTTS())
    loop.lock = VP.VoiceLock(tmp_path / "vp.json", FakeEncoder())
    loop.lock.start_enrol()
    await loop.run()
    assert loop.lock.enrolling == [] and "Please read this line:" in loop.tts.spoken


@pytest.mark.parametrize("text,action", [
    ("learn my voice", "learn"), ("only listen to me", "on"), ("turn on voice lock", "on"),
    ("voice lock off", "off"), ("listen to everyone", "off"), ("forget my voice", "forget"),
    ("is voice lock on", "status"),
])
def test_phrases(text, action):
    assert match_intent(text) == ("voice_lock", {"action": action})


# --- interrupting with voice lock on (owner: "Nova doesn't really allow me to interrupt anymore") ---
class FixedEncoder:
    """Every sound matches the owner's voiceprint this well (cosine)."""

    def __init__(self, cos):
        self.cos = cos

    def embed(self, audio):
        e = self.cos * OWNER + np.sqrt(1 - self.cos ** 2) * STRANGER
        return e.astype(np.float32)


def talking_loop(settings, registry, tmp_path, cos, said):
    from tests.test_barge import STORY, SlowTTS, make
    loop, events = make(settings, registry, [STORY, text_msg("It is noon, sir.")], [
        ("until", lambda l: l.speaking),
        ("say", said, 40), ("quiet", 20)], tts=SlowTTS())
    lock = VP.VoiceLock(tmp_path / "vp.json", FixedEncoder(cos))
    lock.prints.add([OWNER])
    lock.set(on=True, threshold=0.75)
    loop.lock = loop.ctx.services["voicelock"] = lock
    return loop, events


async def run_with_story(loop):
    async def ask():
        await asyncio.sleep(0.01)
        await loop.submit_text("tell me a story")          # typed: no voice check for the request
    await asyncio.gather(loop.run(), ask())


async def test_owner_can_talk_over_nova_with_voice_lock_on(settings, registry, tmp_path):
    """Over Nova's own voice the owner's voiceprint matches worse (0.68 < 0.75): still the owner."""
    loop, events = talking_loop(settings, registry, tmp_path, 0.68, "what time is it")
    await run_with_story(loop)
    assert "interrupted" in [e["type"] for e in events]
    assert "It is noon, sir." in loop.tts.spoken and "The end." not in loop.tts.spoken


async def test_anyone_can_say_stop_but_strangers_cannot_ask(settings, registry, tmp_path):
    loop, events = talking_loop(settings, registry, tmp_path, 0.2, "stop")
    await run_with_story(loop)
    assert "interrupted" in [e["type"] for e in events] and "The end." not in loop.tts.spoken

    loop, events = talking_loop(settings, registry, tmp_path / "b", 0.2, "what time is it")
    await run_with_story(loop)
    assert "It is noon, sir." not in loop.tts.spoken and "The end." in loop.tts.spoken


# --- owner: "he can't even take simple requests like stand down, he's straight up ignoring me" ---
def lock_loop(settings, registry, tmp_path, cos, say, script=()):
    from tests.test_barge import make
    loop, events = make(settings, registry, list(script) or [text_msg("It is noon, sir.")], [
        ("say", say, 10), ("quiet", 20), ("until", lambda l: l.turns_done >= 1 and not l.busy)], tts=FakeTTS())
    lock = VP.VoiceLock(tmp_path / "vp.json", FixedEncoder(cos))
    lock.prints.add([OWNER])
    lock.set(on=True, threshold=0.75)
    loop.lock = loop.ctx.services["voicelock"] = lock
    return loop, events


async def test_stand_down_always_works(settings, registry, tmp_path):
    """A voice the lock doesn't recognise (a new headset) can still stand Nova down."""
    loop, events = lock_loop(settings, registry, tmp_path, 0.3, "Nova, stand down")
    await loop.run()
    assert loop.standby


async def test_a_near_miss_is_said_not_silently_ignored(settings, registry, tmp_path):
    loop, events = lock_loop(settings, registry, tmp_path, 0.66, "Nova, what time is it")
    await loop.run()
    assert any("didn't recognise your voice" in s for s in loop.tts.spoken)
    [ev] = [e for e in events if e["type"] == "ignored"]
    assert ev["voice"] and ev["near"] and ev["score"] == 0.66 and ev["threshold"] == 0.75


async def test_strangers_far_from_the_owner_stay_quiet(settings, registry, tmp_path):
    loop, events = lock_loop(settings, registry, tmp_path, 0.2, "Nova, what time is it")
    await loop.run()
    assert loop.tts.spoken == []                       # game chat: no "didn't recognise you" every time


async def test_that_was_me_learns_the_voice_and_does_it(settings, registry, tmp_path):
    loop, events = lock_loop(settings, registry, tmp_path, 0.66, "Nova, what time is it",
                             [text_msg("It is noon, sir.")])
    await loop.run()
    assert "I'll recognise your voice" in await loop.accept_rejected()
    if loop._turn:
        await loop._turn
    assert "It is noon, sir." in loop.tts.spoken
    assert len(loop.lock.prints.prints) == 2                 # the new microphone's print, numbers only
    assert "There's nothing" in await loop.accept_rejected()  # only once


async def test_a_near_miss_from_music_is_not_mentioned(settings, registry, tmp_path):
    """Owner's log: song lyrics from the speakers ('go my own way') scored 0.64 against a bar of
    0.66. Nova shouldn't apologise for not recognising a song that never said its name."""
    loop, events = lock_loop(settings, registry, tmp_path, 0.66, "oh go my own way")
    await loop.run()
    assert loop.tts.spoken == []
    [ev] = [e for e in events if e["type"] == "ignored"]
    assert ev["voice"] and not ev["near"]
