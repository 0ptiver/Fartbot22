"""Voice lock: recognise the owner's voice, so other people (game chat, TV, visitors) can't
command Nova.

A small speaker-recognition network (Resemblyzer's GE2E voice encoder: 40 mel bands -> 3-layer
LSTM -> 256 numbers) turns a few seconds of speech into a "voiceprint". Same person = similar
numbers. Enrolment ("Nova, learn my voice") averages a few sentences into your print; every
request is then compared to it.

Runs in plain numpy (no PyTorch): the weights come from the Resemblyzer package on PyPI
(checked against a pinned SHA-256) and are converted once into models/voice_encoder.npz.
Privacy: only the 256 numbers are stored (data/voiceprint.json), never any audio.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import pickle
import zipfile
from collections import OrderedDict
from pathlib import Path

import numpy as np

from assistant.core.config import MODELS_DIR, ROOT

log = logging.getLogger(__name__)

WHEEL_URL = ("https://files.pythonhosted.org/packages/d9/b5/d897d5de5b2123a1ad5d32e8e007d83f76582f5c32c60992577263bd7c7e/"
             "Resemblyzer-0.1.4-py3-none-any.whl")
WHEEL_SHA256 = "8f12eb2f1a9982d32e8db7856de754709b59c93a77bcf0ff536584b619a9dd1f"
WEIGHTS_FILE = MODELS_DIR / "voice_encoder.npz"
PRINT_FILE = ROOT / "data" / "voiceprint.json"
RATE = 16000
N_FFT, HOP, N_MELS, PARTIAL = 400, 160, 40, 160


# --- reading the original weights without PyTorch ----------------------------------------------------
class _Storage:
    def __init__(self, dtype, size: int):
        self.dtype, self.size, self.data = dtype, size, None


class _LazyTensor:
    def __init__(self, storage: _Storage, offset: int, size: tuple, stride: tuple):
        self.storage, self.offset, self.shape, self.stride = storage, offset, tuple(size), tuple(stride)

    def array(self) -> np.ndarray:
        base = self.storage.data
        n = int(np.prod(self.shape)) if self.shape else 1
        flat = base[self.offset: self.offset + max(n, 1)]
        contiguous = tuple(int(np.prod(self.shape[i + 1:])) for i in range(len(self.shape)))
        if self.stride == contiguous:
            return flat.reshape(self.shape).copy()
        return np.lib.stride_tricks.as_strided(base[self.offset:], self.shape,
                                               tuple(s * base.itemsize for s in self.stride)).copy()


_DTYPES = {"FloatStorage": np.float32, "DoubleStorage": np.float64, "HalfStorage": np.float16,
           "LongStorage": np.int64, "IntStorage": np.int32, "ByteStorage": np.uint8, "BoolStorage": np.bool_}


class _Unpickler(pickle.Unpickler):
    """Only what a state dict needs; anything else becomes a harmless stub (never executed)."""

    def __init__(self, f, storages: dict):
        super().__init__(f)
        self.storages = storages

    def find_class(self, module, name):
        if module == "collections" and name == "OrderedDict":
            return OrderedDict
        if module == "torch._utils" and name == "_rebuild_tensor_v2":
            return lambda storage, offset, size, stride, *rest: _LazyTensor(storage, offset, size, stride)
        if module == "torch" and name in _DTYPES:
            return _DTYPES[name]
        return lambda *a, **k: None

    def persistent_load(self, pid):
        kind, dtype, key, _location, size = pid[:5]
        if kind != "storage":
            raise pickle.UnpicklingError("unexpected persistent id")
        if key not in self.storages:
            self.storages[key] = _Storage(dtype, size)
        return self.storages[key]


def read_legacy_torch(data: bytes) -> dict:
    """Read a (pre-1.6, non-zip) torch.save file into nested dicts of numpy arrays."""
    f = io.BytesIO(data)
    for _ in range(3):                                     # magic number, protocol version, sys info
        pickle.load(f)
    storages: dict = {}
    obj = _Unpickler(f, storages).load()
    keys = pickle.load(f)
    for key in keys:
        st = storages[key]
        n = int.from_bytes(f.read(8), "little")
        itemsize = np.dtype(st.dtype).itemsize
        st.data = np.frombuffer(f.read(n * itemsize), dtype=np.dtype(st.dtype).newbyteorder("<"))

    def resolve(o):
        if isinstance(o, _LazyTensor):
            return o.array()
        if isinstance(o, dict):
            return {k: resolve(v) for k, v in o.items()}
        return o
    return resolve(obj)


def weights_from_wheel(wheel: bytes) -> dict[str, np.ndarray]:
    if hashlib.sha256(wheel).hexdigest() != WHEEL_SHA256:
        raise ValueError("The voice-lock model download didn't match its expected fingerprint.")
    pt = zipfile.ZipFile(io.BytesIO(wheel)).read("resemblyzer/pretrained.pt")
    state = read_legacy_torch(pt)["model_state"]
    return {k: np.asarray(v, np.float32) for k, v in state.items() if k.startswith(("lstm.", "linear."))}


def fetch_weights(path: Path | None = None) -> Path:
    """Download (once) and convert the model: ~16 MB from PyPI, checked, stored as numpy."""
    path = path or WEIGHTS_FILE
    if path.exists():
        return path
    import httpx
    r = httpx.get(WHEEL_URL, timeout=120, follow_redirects=True)
    r.raise_for_status()
    weights = weights_from_wheel(r.content)
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez(path, **weights)
    return path


# --- the maths -----------------------------------------------------------------------------------------
def _hz_to_mel(f):
    f = np.asanyarray(f, dtype=np.float64)
    f_sp, min_log_hz = 200.0 / 3, 1000.0
    mels = f / f_sp
    min_log_mel, logstep = min_log_hz / f_sp, np.log(6.4) / 27.0
    return np.where(f >= min_log_hz, min_log_mel + np.log(np.maximum(f, 1e-10) / min_log_hz) / logstep, mels)


def _mel_to_hz(m):
    m = np.asanyarray(m, dtype=np.float64)
    f_sp, min_log_hz = 200.0 / 3, 1000.0
    min_log_mel, logstep = min_log_hz / f_sp, np.log(6.4) / 27.0
    return np.where(m >= min_log_mel, min_log_hz * np.exp(logstep * (m - min_log_mel)), f_sp * m)


def mel_filters(sr: int = RATE, n_fft: int = N_FFT, n_mels: int = N_MELS) -> np.ndarray:
    """Slaney-style mel filterbank, as librosa.filters.mel makes it."""
    fft_freqs = np.linspace(0, sr / 2, 1 + n_fft // 2)
    mel_f = _mel_to_hz(np.linspace(_hz_to_mel(0.0), _hz_to_mel(sr / 2), n_mels + 2))
    fdiff = np.diff(mel_f)
    ramps = mel_f[:, None] - fft_freqs[None, :]
    lower = -ramps[:-2] / fdiff[:-1, None]
    upper = ramps[2:] / fdiff[1:, None]
    w = np.maximum(0, np.minimum(lower, upper))
    w *= (2.0 / (mel_f[2: n_mels + 2] - mel_f[:n_mels]))[:, None]
    return w.astype(np.float32)


_MEL = mel_filters()
_WINDOW = (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(N_FFT) / N_FFT)).astype(np.float32)   # periodic Hann


def mel_spectrogram(wav: np.ndarray) -> np.ndarray:
    """(frames, 40) power mel spectrogram, like librosa.feature.melspectrogram (centred frames)."""
    y = np.pad(wav.astype(np.float32), N_FFT // 2)
    n = 1 + (len(y) - N_FFT) // HOP
    frames = np.lib.stride_tricks.as_strided(y, (n, N_FFT), (y.strides[0] * HOP, y.strides[0]))
    spec = np.abs(np.fft.rfft(frames * _WINDOW, axis=1)) ** 2
    return (spec @ _MEL.T).astype(np.float32)


def normalize_volume(wav: np.ndarray, target_dbfs: float = -30.0) -> np.ndarray:
    """Quiet speech is raised to -30 dBFS (never lowered), as the model was trained."""
    rms = float(np.sqrt(np.mean((wav * 32767.0) ** 2))) if wav.size else 0.0
    if rms <= 0:
        return wav
    change = target_dbfs - 20 * np.log10(rms / 32767.0)
    return wav if change < 0 else (wav * 10 ** (change / 20)).astype(np.float32)


def partial_slices(n_samples: int, rate: float = 1.3, min_coverage: float = 0.75) -> list[slice]:
    n_frames = int(np.ceil((n_samples + 1) / HOP))
    step = int(np.round((RATE / rate) / HOP))
    mel_slices = [slice(i, i + PARTIAL) for i in range(0, max(1, n_frames - PARTIAL + step + 1), step)]
    last = mel_slices[-1]
    coverage = (n_samples - last.start * HOP) / (PARTIAL * HOP)
    if coverage < min_coverage and len(mel_slices) > 1:
        mel_slices = mel_slices[:-1]
    return mel_slices


def _sigmoid(x):
    return 0.5 * (1.0 + np.tanh(0.5 * x))          # = 1/(1+e^-x), without overflow


class VoiceEncoder:
    def __init__(self, weights: dict[str, np.ndarray] | Path | None = None):
        if weights is None or isinstance(weights, Path):
            with np.load(weights or fetch_weights()) as z:
                weights = {k: z[k] for k in z.files}
        self.w = weights

    def _forward(self, mels: np.ndarray) -> np.ndarray:
        """(batch, frames, 40) -> (batch, 256) L2-normalised embeddings."""
        x = mels
        for layer in range(3):
            w_ih, w_hh = self.w[f"lstm.weight_ih_l{layer}"], self.w[f"lstm.weight_hh_l{layer}"]
            b = self.w[f"lstm.bias_ih_l{layer}"] + self.w[f"lstm.bias_hh_l{layer}"]
            batch, steps, _ = x.shape
            h = np.zeros((batch, 256), np.float32)
            c = np.zeros((batch, 256), np.float32)
            pre = x @ w_ih.T + b                              # all time steps at once
            outs = np.empty((batch, steps, 256), np.float32)
            for t in range(steps):
                g = pre[:, t] + h @ w_hh.T
                i, f, gg, o = _sigmoid(g[:, :256]), _sigmoid(g[:, 256:512]), np.tanh(g[:, 512:768]), _sigmoid(g[:, 768:])
                c = f * c + i * gg
                h = o * np.tanh(c)
                outs[:, t] = h
            x = outs
        e = np.maximum(h @ self.w["linear.weight"].T + self.w["linear.bias"], 0)
        return e / np.maximum(np.linalg.norm(e, axis=1, keepdims=True), 1e-9)

    def embed(self, wav: np.ndarray) -> np.ndarray:
        """One utterance (16 kHz float) -> 256 numbers."""
        wav = normalize_volume(np.asarray(wav, np.float32))
        slices = partial_slices(len(wav))
        need = slices[-1].stop * HOP
        if need >= len(wav):
            wav = np.pad(wav, (0, need - len(wav)))
        mel = mel_spectrogram(wav)
        partials = self._forward(np.stack([mel[s] for s in slices]))
        e = partials.mean(axis=0)
        return (e / np.linalg.norm(e)).astype(np.float32)


# --- the owner's voiceprint ----------------------------------------------------------------------------
class Voiceprints:
    """Up to 5 prints (e.g. one per microphone). A voice matches if it's close to any of them."""

    MAX = 5

    def __init__(self, path: Path | None = None):
        self.path = path
        self.prints: list[np.ndarray] = []
        try:
            data = json.loads(self._path().read_text(encoding="utf-8"))
            self.prints = [np.asarray(p, np.float32) for p in data.get("prints", [])][: self.MAX]
        except (OSError, ValueError):
            pass

    def _path(self) -> Path:
        return self.path or PRINT_FILE

    @property
    def enrolled(self) -> bool:
        return bool(self.prints)

    def add(self, embeddings: list[np.ndarray]) -> None:
        e = np.mean(embeddings, axis=0)
        self.prints = (self.prints + [(e / np.linalg.norm(e)).astype(np.float32)])[-self.MAX:]
        self.save()

    def forget(self) -> None:
        self.prints = []
        try:
            self._path().unlink()
        except OSError:
            pass

    def save(self) -> None:
        p = self._path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"prints": [np.round(x, 5).tolist() for x in self.prints]}), encoding="utf-8")

    def score(self, embedding: np.ndarray) -> float:
        """Cosine similarity to the closest print (1 = identical)."""
        return max((float(np.dot(embedding, p)) for p in self.prints), default=0.0)


# --- the lock --------------------------------------------------------------------------------------------
ENROL_LINES = [
    "The quick brown fox jumps over the lazy dog.",
    "Nova, set a timer for ten minutes and open Spotify.",
    "I'd like a cup of tea and a biscuit, please.",
    "Tell me when my download finishes, and show the grid.",
    "My favourite game is Grand Theft Auto, and I play it most evenings.",
]
MIN_ENROL_S = 1.2
DEFAULT_THRESHOLD = 0.75


class VoiceLock:
    """Only the owner's voice gets through. Off until a voice has been learned."""

    def __init__(self, path: Path | None = None, encoder: VoiceEncoder | None = None):
        self.prints = Voiceprints(path)
        self._encoder = encoder
        self.on, self.threshold = False, DEFAULT_THRESHOLD
        try:
            data = json.loads(self.prints._path().read_text(encoding="utf-8"))
            self.on = bool(data.get("on", True)) and self.prints.enrolled
            self.threshold = float(data.get("threshold", DEFAULT_THRESHOLD))
        except (OSError, ValueError):
            pass
        self.enrolling: list[np.ndarray] | None = None
        self.last_score: float | None = None

    @property
    def encoder(self) -> VoiceEncoder:
        if self._encoder is None:
            self._encoder = VoiceEncoder()
        return self._encoder

    @property
    def active(self) -> bool:
        return self.on and self.prints.enrolled and self.enrolling is None

    def _save(self) -> None:
        p = self.prints._path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"prints": [np.round(x, 5).tolist() for x in self.prints.prints],
                                 "on": self.on, "threshold": round(self.threshold, 3)}), encoding="utf-8")

    def check(self, audio: np.ndarray) -> tuple[bool, float]:
        """(is it the owner?, similarity). Short phrases get a little leeway."""
        score = self.prints.score(self.encoder.embed(audio))
        self.last_score = score
        leeway = 0.05 if audio.size < RATE * 1.2 else 0.0
        return score >= self.threshold - leeway, score

    # enrolment: "Nova, learn my voice" -> read five sentences
    def start_enrol(self) -> str:
        self.enrolling = []
        return ENROL_LINES[0]

    def add_sample(self, audio: np.ndarray) -> tuple[str | None, str]:
        """(next line to read or None when finished, what to say)."""
        if self.enrolling is None:
            return None, "I'm not learning your voice right now."
        if audio.size < RATE * MIN_ENROL_S:
            return ENROL_LINES[len(self.enrolling)], "That was a bit short. Please read the whole line:"
        self.enrolling.append(self.encoder.embed(audio))
        n = len(self.enrolling)
        if n < len(ENROL_LINES):
            return ENROL_LINES[n], f"Got it, {n} of {len(ENROL_LINES)}. Next:"
        embeds, self.enrolling = self.enrolling, None
        # Calibrate: how alike are your own sentences? Set the bar just under that.
        others = [float(np.dot(e, np.mean([x for x in embeds if x is not e], axis=0) /
                               np.linalg.norm(np.mean([x for x in embeds if x is not e], axis=0)))) for e in embeds]
        self.prints.add(embeds)
        self.threshold = float(np.clip(min(others) - 0.06, 0.62, 0.82))
        self.on = True
        self._save()
        return None, "Done. I know your voice now, and I'll only take orders from you."

    def cancel_enrol(self) -> None:
        self.enrolling = None

    def set(self, on: bool | None = None, threshold: float | None = None) -> None:
        if on is not None:
            self.on = bool(on) and self.prints.enrolled
        if threshold is not None:
            self.threshold = float(np.clip(threshold, 0.5, 0.95))
        self._save()

    def forget(self) -> None:
        self.prints.forget()
        self.on, self.threshold, self.enrolling = False, DEFAULT_THRESHOLD, None
