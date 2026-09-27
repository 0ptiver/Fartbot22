"""Echo cancellation on a simulated room: Nova's voice from the speakers must be removed from
the mic, and the user talking over it must survive."""

import numpy as np
import pytest

from assistant.voice.audio import load_wav_16k, resample
from tests.voice_helpers import WAV

pytest.importorskip("livekit")


def simulate(with_user: bool):
    from assistant.voice.aec import EchoCanceller

    rng = np.random.default_rng(0)
    nova = np.tile(load_wav_16k(WAV), 3)
    nova24 = resample(nova, 16000, 24000)
    ir = np.zeros(4000)
    ir[800] = 0.6                                           # 50 ms path, gain 0.6
    ir[800:] += rng.standard_normal(3200) * np.exp(-np.arange(3200) / 800) * 0.05   # reverb
    echo = np.convolve(nova, ir)[:len(nova)]
    user = np.zeros_like(nova)
    u = load_wav_16k(WAV)[::-1] * 0.8
    start = 6 * 16000
    user[start:start + len(u)] = u[:len(user) - start]
    mic = echo + (user if with_user else 0) + rng.standard_normal(len(nova)) * 0.002
    aec, out, t24 = EchoCanceller(delay_ms=50), [], 0
    for i in range(0, len(mic) - 512, 512):
        n24 = int((i + 512) * 1.5) - t24
        aec.feed_reverse(nova24[t24:t24 + n24], 24000)
        t24 += n24
        out.append(aec.process(mic[i:i + 512]))
    return np.concatenate(out), mic, start


def db(x):
    return 10 * np.log10(np.mean(np.square(x)) + 1e-12)


def test_echo_removed_and_user_kept():
    cleaned, mic, start = simulate(with_user=True)
    alone, _, _ = simulate(with_user=False)
    echo_part = slice(3 * 16000, int(5.5 * 16000))
    assert db(mic[echo_part]) - db(cleaned[echo_part]) > 20          # >20 dB of echo removed
    user_part = slice(start + 4000, start + 28000)
    assert db(cleaned[user_part]) - db(alone[user_part]) > 12        # user clearly above residual


def test_block_sizes_preserved():
    from assistant.voice.aec import EchoCanceller
    aec = EchoCanceller()
    for _ in range(5):
        assert len(aec.process(np.zeros(512, np.float32))) == 512
