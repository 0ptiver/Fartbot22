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
    """Conv weights [2*bins, 1, N]: rows 0..bins-1 give the real part, the rest the imaginary."""
    n = np.arange(frame_length)
    bins = frame_length // 2 + 1 if onesided else frame_length
    k = np.arange(bins)[:, None]
    angle = 2 * np.pi * k * n[None, :] / frame_length
    real = np.cos(angle) * window[None, :]
    imag = -np.sin(angle) * window[None, :]
    return np.concatenate([real, imag])[:, None, :]


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
        graph.initializer.append(numpy_helper.from_array(weights, w_name))
        graph.initializer.append(numpy_helper.from_array(np.array([0, 2, bins, -1], np.int64), shape_name))
        # [B, L] or [B, L, 1] -> [B, 1, L]. (The spec says rank 3; Kokoro's export uses rank 2.)
        graph.initializer.append(numpy_helper.from_array(np.array([0, 1, -1], np.int64), in_shape))
        new_nodes += [
            helper.make_node("Reshape", [signal, in_shape], [f"{base}_t"], name=f"{base}_in"),
            helper.make_node("Conv", [f"{base}_t", w_name], [f"{base}_c"], strides=[step],
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


RANDOM_OPS = {"RandomNormal", "RandomNormalLike", "RandomUniform", "RandomUniformLike", "Multinomial"}


def seeded_copy(src: Path, dst: Path) -> int:
    """Copy a model with a fixed seed on every random op (keyed by node name), so two models
    that differ only in unrelated nodes produce the same random numbers. Returns the count."""
    import zlib

    import onnx
    from onnx import helper

    model = onnx.load(str(src))
    count = 0
    for node in model.graph.node:
        if node.op_type in RANDOM_OPS:
            for a in list(node.attribute):
                if a.name == "seed":
                    node.attribute.remove(a)
            node.attribute.append(helper.make_attribute("seed", float(zlib.crc32(node.name.encode()) % 100000)))
            count += 1
    onnx.save(model, str(dst))
    return count


def verify_kokoro(original: Path, converted: Path, voices: Path, voice: str, lang: str) -> dict:
    """Kokoro adds random noise while generating, so two runs never match sample-for-sample.
    Instead: (1) the STFT maths must match exactly, (2) the converted model must produce
    speech of the same length and loudness, (3) report the original's run-to-run noise."""
    import onnx
    import onnxruntime as ort
    from kokoro_onnx import Kokoro

    specs = stft_specs(onnx.load(str(original)))
    math_err = max((stft_math_error(sp) for sp in specs), default=0.0)

    text = "Good evening, sir. The time is a quarter past eleven."
    audio = {}
    for tag, path in (("orig", original), ("orig2", original), ("conv", converted)):
        sess = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
        audio[tag], _ = Kokoro.from_session(sess, str(voices)).create(text, voice=voice, lang=lang, trim=False)

    def rms(a):
        return float(np.sqrt(np.mean(np.square(a)))) if len(a) else 0.0

    # Definitive check: same fixed random numbers in both -> audio must match.
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        so, sc = Path(d) / "o.onnx", Path(d) / "c.onnx"
        n_random = seeded_copy(original, so)
        seeded_copy(converted, sc)
        seeded = []
        for path in (so, sc):
            sess = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
            seeded.append(Kokoro.from_session(sess, str(voices)).create(text, voice=voice, lang=lang, trim=False)[0])
    m = min(len(seeded[0]), len(seeded[1]))
    seeded_diff = (float(np.max(np.abs(seeded[0][:m] - seeded[1][:m])) / max(float(np.max(np.abs(seeded[0]))), 1e-9))
                   if m and len(seeded[0]) == len(seeded[1]) else float("inf"))

    n = min(len(audio["orig"]), len(audio["orig2"]))
    return {
        "random_ops": n_random,
        "seeded_diff": seeded_diff,
        "stft_math_error": math_err,
        "length_ratio": len(audio["conv"]) / max(len(audio["orig"]), 1),
        "loudness_ratio": rms(audio["conv"]) / max(rms(audio["orig"]), 1e-9),
        "original_run_to_run_diff": float(np.max(np.abs(audio["orig"][:n] - audio["orig2"][:n]))) if n else 0.0,
    }


def verification_ok(v: dict) -> bool:
    # With random numbers fixed, the audio must match; the length/loudness checks remain as
    # a sanity net in case the model has no random ops to fix.
    return (v["stft_math_error"] < 1e-4 and v.get("seeded_diff", 0.0) < 1e-3
            and 0.9 <= v["length_ratio"] <= 1.1 and 0.75 <= v["loudness_ratio"] <= 1.33)
