"""Make the Kokoro ONNX model run fully on the GPU.

onnxruntime's CUDA provider has no STFT kernel, so Kokoro's STFT runs on the CPU
(~85 ms per sentence on the owner's PC, plus GPU<->CPU copies). An STFT with a fixed
window is exactly a strided 1-D convolution with windowed cosine/sine kernels, which
the GPU runs fast. This rewrites each STFT node into Reshape -> Conv -> Reshape ->
Transpose, then checks the new model gives the same audio before it's used.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np


def _const_value(graph, name: str, producers: dict):
    """Value of a constant tensor: initializer, Constant node, or a window op of constant size."""
    from onnx import numpy_helper

    if not name:
        return None
    for init in graph.initializer:
        if init.name == name:
            return numpy_helper.to_array(init)
    node = producers.get(name)
    if node is None:
        return None
    if node.op_type == "Constant":
        for a in node.attribute:
            if a.name == "value":
                return numpy_helper.to_array(a.t)
        return None
    if node.op_type in ("HannWindow", "HammingWindow", "BlackmanWindow"):
        size = _const_value(graph, node.input[0], producers)
        if size is None:
            return None
        periodic = next((a.i for a in node.attribute if a.name == "periodic"), 1)
        n = int(np.asarray(size).reshape(-1)[0])
        denom = n if periodic else n - 1
        k = np.arange(n) * 2 * np.pi / denom
        if node.op_type == "HannWindow":
            w = 0.5 - 0.5 * np.cos(k)
        elif node.op_type == "HammingWindow":
            w = 25 / 46 - (21 / 46) * np.cos(k)
        else:
            w = 0.42 - 0.5 * np.cos(k) + 0.08 * np.cos(2 * k)
        return w.astype(np.float32)
    return None


def dft_kernels(window: np.ndarray, frame_length: int, onesided: bool) -> np.ndarray:
    """Conv weights [2*bins, 1, N]: rows 0..bins-1 give the real part, the rest the imaginary.

    Values that are mathematically zero must be *exactly* zero. In particular the imaginary
    parts of the 0 Hz and Nyquist bins: an FFT returns exact 0 there, and Kokoro takes the
    phase with atan(imag / real) plus sign tests, so a stray 1e-9 flips the phase by pi.
    Angles are reduced to exact multiples of 2*pi/N first so sin/cos land on exact values."""
    n = np.arange(frame_length)
    bins = frame_length // 2 + 1 if onesided else frame_length
    k = np.arange(bins)[:, None]
    m = (k * n[None, :]) % frame_length           # exact integer phase index
    angle = 2 * np.pi * m / frame_length
    cos, sin = np.cos(angle), np.sin(angle)
    cos[np.abs(cos) < 1e-12] = 0.0
    sin[np.abs(sin) < 1e-12] = 0.0
    sin[(2 * m) % frame_length == 0] = 0.0        # m = 0 or N/2: sin is exactly zero
    real = cos * window[None, :]
    imag = -sin * window[None, :]
    imag[imag == 0] = 0.0                         # no negative zeros
    return np.concatenate([real, imag])[:, None, :]


# PyTorch (where Kokoro was trained) returns exactly +0 for the imaginary part of the 0 Hz
# and Nyquist bins, so torch.angle gives +pi there for negative values. The exported model
# computes the angle with atan + "imag > 0" tests, which turns an exact 0 into -pi, and
# onnxruntime's own STFT leaves random +/-1e-6 noise there. A tiny positive value restores
# PyTorch's +pi without measurably changing magnitudes.
PHASE_EPS = 1e-20


def self_conjugate_bins(frame_length: int, bins: int) -> list[int]:
    """Bins whose imaginary part is exactly zero for real input (0 Hz and Nyquist)."""
    return [k for k in range(bins) if (2 * k) % frame_length == 0]


def stft_spec(graph, node, producers) -> dict | None:
    """Constant parameters of an STFT node, or None if they aren't constant."""
    ins = list(node.input) + [""] * (4 - len(node.input))
    _, step_name, window_name, length_name = ins[:4]
    step = _const_value(graph, step_name, producers)
    window = _const_value(graph, window_name, producers)
    length = _const_value(graph, length_name, producers)
    if step is None or (window is None and length is None):
        return None
    frame_length = int(np.asarray(length).reshape(-1)[0]) if length is not None else len(window)
    return {
        "name": node.name or "STFT",
        "step": int(np.asarray(step).reshape(-1)[0]),
        "frame_length": frame_length,
        "window": window if window is not None else np.ones(frame_length, np.float32),
        "has_window": window is not None,
        "onesided": bool(next((a.i for a in node.attribute if a.name == "onesided"), 1)),
    }


def stft_specs(model) -> list[dict]:
    graph = model.graph
    producers = {o: n for n in graph.node for o in n.output}
    return [spec for n in graph.node if n.op_type == "STFT"
            if (spec := stft_spec(graph, n, producers)) is not None]


def replace_stft(model) -> list[str]:
    """Rewrite STFT nodes in place. Returns a report line per node."""
    from onnx import helper, numpy_helper

    graph = model.graph
    producers = {o: n for n in graph.node for o in n.output}
    report = []
    new_nodes = []
    for node in graph.node:
        if node.op_type != "STFT":
            new_nodes.append(node)
            continue
        signal = node.input[0]
        spec = stft_spec(graph, node, producers)
        if spec is None:
            report.append(f"{node.name or 'STFT'}: left on CPU (window/step not constant)")
            new_nodes.append(node)
            continue
        step, frame_length, window, onesided = (spec["step"], spec["frame_length"],
                                                spec["window"], spec["onesided"])
        dtype = window.dtype if window.dtype in (np.float16, np.float32) else np.float32
        weights = dft_kernels(window.astype(np.float64), frame_length, onesided).astype(dtype)
        bins = weights.shape[0] // 2

        base = (node.name or "stft").replace("/", "_") + "_gpu"
        w_name, shape_name, in_shape = f"{base}_W", f"{base}_shape", f"{base}_in_shape"
        b_name = f"{base}_B"
        bias = np.zeros(weights.shape[0], dtype)
        for k in self_conjugate_bins(frame_length, bins):
            bias[bins + k] = PHASE_EPS
        graph.initializer.append(numpy_helper.from_array(weights, w_name))
        graph.initializer.append(numpy_helper.from_array(bias, b_name))
        graph.initializer.append(numpy_helper.from_array(np.array([0, 2, bins, -1], np.int64), shape_name))
        # [B, L] or [B, L, 1] -> [B, 1, L]. (The spec says rank 3; Kokoro's export uses rank 2.)
        graph.initializer.append(numpy_helper.from_array(np.array([0, 1, -1], np.int64), in_shape))
        new_nodes += [
            helper.make_node("Reshape", [signal, in_shape], [f"{base}_t"], name=f"{base}_in"),
            helper.make_node("Conv", [f"{base}_t", w_name, b_name], [f"{base}_c"], strides=[step],
                             name=f"{base}_conv"),
            helper.make_node("Reshape", [f"{base}_c", shape_name], [f"{base}_r"], name=f"{base}_reshape"),
            helper.make_node("Transpose", [f"{base}_r"], [node.output[0]], perm=[0, 3, 2, 1],
                             name=f"{base}_out"),
        ]
        report.append(f"{node.name or 'STFT'}: -> Conv (frame {frame_length}, hop {step}, {bins} bins)")
    del graph.node[:]
    graph.node.extend(new_nodes)
    _prune_unused(graph)
    return report


def _prune_unused(graph) -> None:
    """Drop constants only the old STFT nodes used (avoids onnxruntime warnings)."""
    while True:
        used = {i for n in graph.node for i in n.input} | {o.name for o in graph.output}
        dead_nodes = [n for n in graph.node if n.op_type == "Constant" and n.output[0] not in used]
        dead_inits = [i for i in graph.initializer if i.name not in used]
        if not dead_nodes and not dead_inits:
            return
        for n in dead_nodes:
            graph.node.remove(n)
        for i in dead_inits:
            graph.initializer.remove(i)


def convert(src: Path, dst: Path) -> list[str]:
    import onnx

    model = onnx.load(str(src))
    report = replace_stft(model)
    onnx.checker.check_model(model)
    onnx.save(model, str(dst))
    return report


def stft_math_error(spec: dict) -> float:
    """Run the original STFT and its Conv replacement on the same test signal (with the
    model's real window/hop/length) and return the largest relative difference."""
    import onnxruntime as ort
    from onnx import TensorProto, helper, numpy_helper

    rng = np.random.default_rng(0)
    t = np.arange(24000) / 24000
    signal = (0.5 * np.sin(2 * np.pi * 220 * t) + 0.2 * np.sin(2 * np.pi * 3100 * t)
              + 0.05 * rng.standard_normal(t.size)).astype(np.float32)[None, :]
    last = None
    for rank in (2, 3):
        x = signal if rank == 2 else signal[..., None]
        inits = [numpy_helper.from_array(np.array(spec["step"], np.int64), "step"),
                 numpy_helper.from_array(np.array(spec["frame_length"], np.int64), "flen")]
        win = ""
        if spec["has_window"]:
            inits.append(numpy_helper.from_array(spec["window"].astype(np.float32), "win"))
            win = "win"
        node = helper.make_node("STFT", ["x", "step", win, "flen"], ["y"], onesided=int(spec["onesided"]))
        graph = helper.make_graph([node], "stft", [helper.make_tensor_value_info("x", TensorProto.FLOAT, list(x.shape))],
                                  [helper.make_tensor_value_info("y", TensorProto.FLOAT, [None] * 4)], inits)
        model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 17)], ir_version=9)
        try:
            want = ort.InferenceSession(model.SerializeToString(), providers=["CPUExecutionProvider"]).run(None, {"x": x})[0]
        except Exception as e:  # this runtime doesn't accept this input rank
            last = e
            continue
        replace_stft(model)
        got = ort.InferenceSession(model.SerializeToString(), providers=["CPUExecutionProvider"]).run(None, {"x": x})[0]
        if got.shape != want.shape:
            return float("inf")
        return float(np.max(np.abs(got - want)) / max(float(np.max(np.abs(want))), 1e-9))
    raise RuntimeError(f"STFT check failed: {last}")


def make_reference(src: Path, dst: Path) -> None:
    """The original model (onnxruntime's own STFT) with only the 0 Hz / Nyquist imaginary
    parts set the PyTorch way. The GPU version must match this exactly."""
    import onnx
    from onnx import helper, numpy_helper

    model = onnx.load(str(src))
    graph = model.graph
    for spec_node in [n for n in graph.node if n.op_type == "STFT"]:
        spec = stft_spec(graph, spec_node, {o: n for n in graph.node for o in n.output})
        if spec is None:
            continue
        bins = spec["frame_length"] // 2 + 1 if spec["onesided"] else spec["frame_length"]
        mask = np.ones((1, 1, bins, 2), np.float32)
        add = np.zeros((1, 1, bins, 2), np.float32)
        for k in self_conjugate_bins(spec["frame_length"], bins):
            mask[0, 0, k, 1], add[0, 0, k, 1] = 0.0, PHASE_EPS
        out = spec_node.output[0]
        raw, masked = out + "_raw", out + "_masked"
        spec_node.output[0] = raw
        base = (spec_node.name or "stft").replace("/", "_") + "_ref"
        graph.initializer.extend([numpy_helper.from_array(mask, base + "_mask"),
                                  numpy_helper.from_array(add, base + "_add")])
        idx = list(graph.node).index(spec_node) + 1
        graph.node.insert(idx, helper.make_node("Mul", [raw, base + "_mask"], [masked]))
        graph.node.insert(idx + 1, helper.make_node("Add", [masked, base + "_add"], [out]))
    onnx.save(model, str(dst))


def _speak(path: Path, voices: Path, voice: str, lang: str, text: str):
    import onnxruntime as ort
    from kokoro_onnx import Kokoro

    sess = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
    return Kokoro.from_session(sess, str(voices)).create(text, voice=voice, lang=lang, trim=False)[0]


def verify_kokoro(original: Path, converted: Path, voices: Path, voice: str, lang: str,
                  save_dir: Path | None = None) -> dict:
    """(1) STFT maths vs onnxruntime on a test signal, (2) full-model audio vs the reference
    (original STFT + PyTorch-style 0 Hz/Nyquist phase) must match, (3) loudness report."""
    import tempfile

    import onnx

    specs = stft_specs(onnx.load(str(original)))
    math_err = max((stft_math_error(sp) for sp in specs), default=0.0)
    text = "Good evening, sir. The time is a quarter past eleven."
    with tempfile.TemporaryDirectory() as d:
        ref_path = Path(d) / "ref.onnx"
        make_reference(original, ref_path)
        orig, ref, conv = (_speak(p, voices, voice, lang, text) for p in (original, ref_path, converted))

    def rms(a):
        return float(np.sqrt(np.mean(np.square(a)))) if len(a) else 0.0

    n = min(len(ref), len(conv))
    ref_diff = (float(np.max(np.abs(ref[:n] - conv[:n])) / max(float(np.max(np.abs(ref))), 1e-9))
                if n and len(ref) == len(conv) else float("inf"))
    # Sample-exact agreement is impossible: Kokoro takes atan(imag/real) of the STFT, and where
    # real is ~0 any two STFT implementations disagree on its sign (a pi phase flip). So judge
    # the *sound*: spectral distance to the reference, compared with how far the original
    # model itself is from the reference.
    spec_conv = spectral_distance_db(ref, conv)
    spec_orig = spectral_distance_db(ref, orig)
    if save_dir is not None:
        import soundfile as sf
        save_dir.mkdir(parents=True, exist_ok=True)
        sf.write(str(save_dir / "1_original.wav"), orig, 24000)
        sf.write(str(save_dir / "2_fast_gpu.wav"), conv, 24000)
    return {
        "stft_math_error": math_err,
        "reference_diff": ref_diff,
        "length_ratio": len(conv) / max(len(orig), 1),
        "loudness_vs_original": rms(conv) / max(rms(orig), 1e-9),
        "loudness_vs_reference": rms(conv) / max(rms(ref), 1e-9),
        "spectral_db": spec_conv,
        "original_spectral_db": spec_orig,
    }


def spectral_distance_db(a: np.ndarray, b: np.ndarray, n_fft: int = 1024, hop: int = 256) -> float:
    """Mean absolute difference of log-magnitude spectrograms in dB (ignores phase)."""
    n = min(len(a), len(b))
    if n < n_fft:
        return float("inf")
    win = np.hanning(n_fft)

    def spec(x):
        frames = np.lib.stride_tricks.sliding_window_view(x[:n], n_fft)[::hop] * win
        return 20 * np.log10(np.abs(np.fft.rfft(frames, axis=-1)) + 1e-5)

    sa, sb = spec(np.asarray(a, np.float64)), spec(np.asarray(b, np.float64))
    loud = sa > sa.max() - 60          # only where there is sound (ignore the silent floor)
    return float(np.mean(np.abs(sa - sb)[loud])) if loud.any() else 0.0


def verification_ok(v: dict) -> bool:
    if v["stft_math_error"] >= 1e-4 or not 0.98 <= v["length_ratio"] <= 1.02:
        return False
    if v["reference_diff"] < 1e-3:              # identical: nothing more to check
        return True
    return (0.95 <= v["loudness_vs_reference"] <= 1.05
            and v["spectral_db"] <= max(1.0, 1.5 * v["original_spectral_db"]))
