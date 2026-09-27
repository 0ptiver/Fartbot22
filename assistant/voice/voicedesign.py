"""Nova's own voice: a blend of Kokoro voices, plus speed, pitch and accent.

Kokoro describes each voice as a "style" (a small array of numbers); mixing styles with
weights gives a voice in between them. Pitch isn't a Kokoro setting, so it's done after:
the audio is generated a little faster/slower and resampled back, which moves the pitch
without changing how quickly Nova talks.
Saved in data/voice.json; config.yaml's voice/speed are the fallback.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

from assistant.core.config import ROOT

VOICE_FILE = ROOT / "data" / "voice.json"
MAX_VOICES = 3
_ACCENT = {"a": "American", "b": "British"}
_GENDER = {"f": "woman", "m": "man"}

PRESETS = {
    "Butler": {"mix": {"bm_george": 1.0}, "speed": 1.05, "pitch": 0.0, "lang": "en-gb"},
    "Deep butler": {"mix": {"bm_george": 0.6, "bm_lewis": 0.4}, "speed": 1.0, "pitch": -1.5, "lang": "en-gb"},
    "Young gent": {"mix": {"bm_fable": 0.6, "bm_daniel": 0.4}, "speed": 1.08, "pitch": 0.0, "lang": "en-gb"},
    "Lady Nova": {"mix": {"bf_emma": 0.7, "bf_isabella": 0.3}, "speed": 1.05, "pitch": 0.0, "lang": "en-gb"},
    "Nova (American)": {"mix": {"af_nova": 0.6, "af_heart": 0.4}, "speed": 1.05, "pitch": 0.0, "lang": "en-us"},
    "Movie trailer": {"mix": {"am_onyx": 0.5, "bm_lewis": 0.5}, "speed": 0.95, "pitch": -3.0, "lang": "en-us"},
}


@dataclass
class VoiceDesign:
    mix: dict[str, float] = field(default_factory=lambda: {"bm_george": 1.0})
    speed: float = 1.05          # 0.7 .. 1.4
    pitch: float = 0.0           # semitones, -4 .. +4
    lang: str = "en-gb"          # en-gb (British pronunciation) | en-us

    def clean(self, available: list[str] | None = None) -> "VoiceDesign":
        """Keep it sane: known voices, at most three, weights summing to 1, limits on the rest."""
        mix = {k: float(v) for k, v in self.mix.items()
               if isinstance(k, str) and re.fullmatch(r"[a-z]{2}_[a-z]+", k) and float(v) > 0.01}
        if available is not None:
            mix = {k: v for k, v in mix.items() if k in available}
        mix = dict(sorted(mix.items(), key=lambda kv: -kv[1])[:MAX_VOICES]) or {"bm_george": 1.0}
        total = sum(mix.values())
        return VoiceDesign({k: round(v / total, 3) for k, v in mix.items()},
                           float(min(max(self.speed, 0.7), 1.4)), float(min(max(self.pitch, -4.0), 4.0)),
                           self.lang if self.lang in ("en-gb", "en-us") else "en-gb")

    @classmethod
    def from_dict(cls, d: dict) -> "VoiceDesign":
        return cls(dict(d.get("mix") or {}), float(d.get("speed", 1.05)), float(d.get("pitch", 0.0)),
                   str(d.get("lang", "en-gb"))).clean()

    @classmethod
    def load(cls, fallback: "VoiceDesign", path: Path | None = None) -> "VoiceDesign":
        path = path or VOICE_FILE
        try:
            return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            return fallback

    def save(self, path: Path | None = None) -> None:
        path = path or VOICE_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=1), encoding="utf-8")
        tmp.replace(path)


def catalog(names: list[str]) -> list[dict[str, str]]:
    """English voices with friendly labels: 'George (British man)'."""
    out = []
    for n in sorted(names):
        m = re.fullmatch(r"([ab])([fm])_([a-z]+)", n)
        if m:
            out.append({"id": n, "label": f"{m.group(3).capitalize()} ({_ACCENT[m.group(1)]} {_GENDER[m.group(2)]})"})
    return out


def blend(style_of, mix: dict[str, float]) -> np.ndarray | str:
    """One voice: its name. Several: the weighted average of their styles."""
    if len(mix) == 1:
        return next(iter(mix))
    return sum(np.asarray(style_of(name), dtype=np.float32) * w for name, w in mix.items()).astype(np.float32)


def pitch_factor(semitones: float) -> float:
    return float(2 ** (semitones / 12))


def repitch(audio: np.ndarray, semitones: float) -> np.ndarray:
    """Resample so the pitch moves by `semitones` (the audio gets shorter/longer by the same
    factor; the caller asks Kokoro for correspondingly slower/faster speech to cancel that)."""
    if abs(semitones) < 0.05 or audio.size < 2:
        return audio
    f = pitch_factor(semitones)
    n = max(2, int(round(audio.size / f)))
    x = np.linspace(0, audio.size - 1, n)
    return np.interp(x, np.arange(audio.size), audio).astype(np.float32)
