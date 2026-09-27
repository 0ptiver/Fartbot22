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
        ins = list(node.input) + [""] * (4 - len(node.input))
        signal, step_name, window_name, length_name = ins[:4]
        step = _const_value(graph, step_name, producers)
        window = _const_value(graph, window_name, producers)
        length = _const_value(graph, length_name, producers)
        onesided = bool(next((a.i for a in node.attribute if a.name == "onesided"), 1))
        if step is None or (window is None and length is None):
            report.append(f"{node.name or 'STFT'}: left on CPU (window/step not constant)")
            new_nodes.append(node)
            continue
        step = int(np.asarray(step).reshape(-1)[0])
        frame_length = int(np.asarray(length).reshape(-1)[0]) if length is not None else len(window)
        if window is None:
            window = np.ones(frame_length, np.float32)
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
    return report


def convert(src: Path, dst: Path) -> list[str]:
    import onnx

    model = onnx.load(str(src))
    report = replace_stft(model)
    onnx.checker.check_model(model)
    onnx.save(model, str(dst))
    return report


def verify_kokoro(original: Path, converted: Path, voices: Path, voice: str, lang: str) -> float:
    """Synthesize the same sentence with both models (CPU) and return the max audio difference."""
    import onnxruntime as ort
    from kokoro_onnx import Kokoro

    outs = []
    for path in (original, converted):
        sess = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
        k = Kokoro.from_session(sess, str(voices))
        audio, _ = k.create("Good evening, sir. The time is a quarter past eleven.",
                            voice=voice, lang=lang, trim=False)
        outs.append(audio)
    n = min(len(outs[0]), len(outs[1]))
    return float(np.max(np.abs(outs[0][:n] - outs[1][:n]))) if n else 1.0
