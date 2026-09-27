"""Model file locations and one-shot downloader (`python -m assistant models`)."""

from __future__ import annotations

import io
import json
import urllib.request
import zipfile
from pathlib import Path

from assistant.core.config import MODELS_DIR

KOKORO_BASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"
KOKORO_FILES = ("kokoro-v1.0.onnx", "voices-v1.0.bin")


def silero_path() -> Path:
    return MODELS_DIR / "silero_vad.onnx"


def kokoro_paths() -> tuple[Path, Path]:
    return MODELS_DIR / KOKORO_FILES[0], MODELS_DIR / KOKORO_FILES[1]


def _download(url: str, dest: Path) -> None:
    print(f"  downloading {url}")
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(url) as r, tmp.open("wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while chunk := r.read(1 << 20):
            f.write(chunk)
            done += len(chunk)
            if total:
                print(f"\r    {done * 100 // total}% of {total >> 20} MB", end="", flush=True)
    print()
    tmp.replace(dest)


def fetch_silero() -> Path:
    """Silero VAD ships inside its PyPI wheel; fetch the wheel and extract the ONNX model."""
    dest = silero_path()
    if dest.exists():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    meta = json.load(urllib.request.urlopen("https://pypi.org/pypi/silero-vad/json"))
    wheel = next(u for u in meta["urls"] if u["filename"].endswith(".whl"))
    print(f"  downloading {wheel['filename']}")
    data = urllib.request.urlopen(wheel["url"]).read()
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        dest.write_bytes(z.read("silero_vad/data/silero_vad.onnx"))
    return dest


def fetch_all(include_whisper_model: str | None = None) -> None:
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    print("Silero VAD:")
    print("  ok:", fetch_silero())
    print("Kokoro TTS:")
    for name in KOKORO_FILES:
        dest = MODELS_DIR / name
        if not dest.exists():
            _download(KOKORO_BASE + name, dest)
        print("  ok:", dest)
    if include_whisper_model:
        print(f"Whisper {include_whisper_model} (from Hugging Face, cached by faster-whisper):")
        from faster_whisper.utils import download_model
        print("  ok:", download_model(include_whisper_model))
