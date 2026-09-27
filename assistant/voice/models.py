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
KOKORO_INT8 = "kokoro-v1.0.int8.onnx"   # ~90 MB, smaller; was slower on the owner's CPU
KOKORO_FP16 = "kokoro-v1.0.fp16.onnx"   # ~170 MB, half precision (was slower on the owner's GPU)
KOKORO_GPU = "kokoro-v1.0.gpu.onnx"     # made locally: STFT rewritten so it runs on the GPU


def silero_path() -> Path:
    return MODELS_DIR / "silero_vad.onnx"


def kokoro_paths(model_file: str | None = None) -> tuple[Path, Path]:
    return MODELS_DIR / (model_file or KOKORO_FILES[0]), MODELS_DIR / KOKORO_FILES[1]


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
    make_gpu_kokoro()
    print("Voice lock (voice recognition):")
    try:
        from assistant.voice.voiceprint import fetch_weights
        print("  ok:", fetch_weights())
    except Exception as e:                        # optional: only voice lock needs it
        print(f"  skipped ({e}); voice lock will try again when you use it")
    if include_whisper_model:
        print(f"Whisper {include_whisper_model} (from Hugging Face, cached by faster-whisper):")
        from faster_whisper.utils import download_model
        print("  ok:", download_model(include_whisper_model))


def make_gpu_kokoro(voice: str = "bm_george", lang: str = "en-gb", recheck: bool = False) -> Path | None:
    """Create kokoro-v1.0.gpu.onnx (STFT -> Conv) and keep it only if the audio matches."""
    from assistant.voice.tts.onnx_fix import convert, verification_ok, verify_kokoro

    src, voices = kokoro_paths()
    dst = MODELS_DIR / KOKORO_GPU
    if recheck and dst.exists():
        print("Re-checking the Kokoro GPU version:")
        v = verify_kokoro(src, dst, voices, voice, lang, save_dir=_compare_dir())
        _print_verification(v)
        if not verification_ok(v):
            dst.unlink()
            print("  removed it; Nova will use the original model")
            return None
        print("  ok")
        return dst
    if dst.exists() or not src.exists():
        return dst if dst.exists() else None
    print("Kokoro GPU version (moves the STFT step from CPU to GPU):")
    tmp = dst.with_suffix(".tmp.onnx")
    try:
        for line in convert(src, tmp):
            print("  " + line)
        v = verify_kokoro(src, tmp, voices, voice, lang, save_dir=_compare_dir())
    except Exception as e:
        print(f"  skipped: {type(e).__name__}: {e}")
        tmp.unlink(missing_ok=True)
        return None
    _print_verification(v)
    if not verification_ok(v):
        print("  not using it")
        tmp.unlink(missing_ok=True)
        return None
    tmp.replace(dst)
    print(f"  ok: {dst}")
    return dst


def _compare_dir() -> Path:
    from assistant.core.config import ROOT
    return ROOT / "data" / "voice_compare"


def _print_verification(v: dict) -> None:
    print(f"  STFT maths error {v['stft_math_error']:.1e} (must be < 1e-4)")
    print(f"  sound vs reference: spectral difference {v.get('spectral_db', float('nan')):.2f} dB "
          f"(original model itself: {v.get('original_spectral_db', float('nan')):.2f} dB; "
          f"must be <= max(1.0, 1.5x that))")
    print(f"  loudness vs original x{v['loudness_vs_original']:.2f}, vs reference "
          f"x{v['loudness_vs_reference']:.2f}; length x{v['length_ratio']:.2f}")
    print(f"  listen: {_compare_dir() / '1_original.wav'}  vs  {_compare_dir() / '2_fast_gpu.wav'}")
