"""`python -m assistant models --debug-stft`: compare the real data flowing into and out of
Kokoro's STFT step in the original model vs the Conv rewrite, to find where they differ."""

from __future__ import annotations

import tempfile
from pathlib import Path

import numpy as np


def _with_outputs(model_path: Path, names: list[str], dst: Path) -> None:
    import onnx
    from onnx import TensorProto, helper

    model = onnx.load(str(model_path))
    existing = {o.name for o in model.graph.output}
    for n in names:
        if n and n not in existing:
            model.graph.output.append(helper.make_tensor_value_info(n, TensorProto.FLOAT, None))
    onnx.save(model, str(dst))


def _capture_inputs(model_path: Path, voices: Path, voice: str, lang: str, text: str) -> dict:
    import onnxruntime as ort
    from kokoro_onnx import Kokoro

    sess = ort.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])
    captured = {}
    real_run = sess.run

    def spy(output_names, feeds, *a, **k):
        captured.update(feeds)
        return real_run(output_names, feeds, *a, **k)

    sess.run = spy
    Kokoro.from_session(sess, str(voices)).create(text, voice=voice, lang=lang, trim=False)
    return captured


def debug_stft(original: Path, voices: Path, voice: str = "bm_george", lang: str = "en-gb") -> None:
    import onnx
    import onnxruntime as ort

    from assistant.voice.tts.onnx_fix import replace_stft, stft_specs

    model = onnx.load(str(original))
    stft = next(n for n in model.graph.node if n.op_type == "STFT")
    consumers = [n.op_type for n in model.graph.node if stft.output[0] in n.input]
    print(f"STFT node: {stft.name}")
    print(f"  inputs: {list(stft.input)}")
    print(f"  attributes: {[(a.name, onnx.helper.get_attribute_value(a)) for a in stft.attribute]}")
    print(f"  output used by: {consumers}")
    spec = stft_specs(model)[0]
    w = spec["window"]
    print(f"  window: len {len(w)} dtype {w.dtype} first {np.round(w[:4], 4).tolist()} "
          f"sum {w.sum():.4f}; step {spec['step']}; frame_length {spec['frame_length']}; onesided {spec['onesided']}")

    text = "Good evening, sir. The time is a quarter past eleven."
    feeds = _capture_inputs(original, voices, voice, lang, text)
    print(f"  model inputs: { {k: (v.shape, str(v.dtype)) for k, v in feeds.items()} }")

    with tempfile.TemporaryDirectory() as d:
        conv_path = Path(d) / "conv.onnx"
        replace_stft(model)
        onnx.save(model, str(conv_path))
        tap_o, tap_c = Path(d) / "o.onnx", Path(d) / "c.onnx"
        names = [stft.input[0], stft.output[0]]
        _with_outputs(original, names, tap_o)
        _with_outputs(conv_path, names, tap_c)
        res = {}
        for tag, path in (("orig", tap_o), ("conv", tap_c)):
            sess = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
            outs = sess.run(None, feeds)
            got = {o.name: v for o, v in zip(sess.get_outputs(), outs)}
            res[tag] = (got[stft.input[0]], got[stft.output[0]], outs[0].ravel())

    (sig_o, y_o, audio_o), (sig_c, y_c, audio_c) = res["orig"], res["conv"]
    print(f"\nSTFT input signal: shape {sig_o.shape} range [{sig_o.min():.3f}, {sig_o.max():.3f}]; "
          f"identical in both models: {np.array_equal(sig_o, sig_c)}")
    print(f"STFT output: original {y_o.shape}, rewrite {y_c.shape}")
    if y_o.shape == y_c.shape:
        rel = np.max(np.abs(y_o - y_c)) / max(np.max(np.abs(y_o)), 1e-9)
        print(f"  max relative difference: {rel:.2e}")
        if rel > 1e-4:
            re_o, im_o, re_c, im_c = y_o[..., 0], y_o[..., 1], y_c[..., 0], y_c[..., 1]
            print(f"  real part diff {np.max(np.abs(re_o - re_c)):.3g}, imag diff {np.max(np.abs(im_o - im_c)):.3g}, "
                  f"imag sign-flipped diff {np.max(np.abs(im_o + im_c)):.3g}")
            per_frame = np.max(np.abs(y_o - y_c), axis=(0, 2, 3))
            print(f"  worst frames: {np.argsort(per_frame)[-5:].tolist()} of {len(per_frame)}")
            print(f"  original frame 0 bin 0..3: {np.round(y_o[0, 0, :4], 4).tolist()}")
            print(f"  rewrite  frame 0 bin 0..3: {np.round(y_c[0, 0, :4], 4).tolist()}")
            # What does a standalone STFT give on this exact signal?
            from assistant.voice.tts.onnx_fix import stft_math_error
            print(f"  standalone check with synthetic signal: {stft_math_error(spec):.2e}")
    n = min(len(audio_o), len(audio_c))
    print(f"Final audio max difference: {np.max(np.abs(audio_o[:n] - audio_c[:n])):.3g}")
