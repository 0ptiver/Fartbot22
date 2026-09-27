"""The STFT -> Conv rewrite must match onnxruntime's STFT exactly."""

import numpy as np
import onnx
import onnxruntime as ort
import pytest
from onnx import TensorProto, helper, numpy_helper

from assistant.voice.tts.onnx_fix import replace_stft


def stft_model(window_kind: str, onesided: int = 1, n: int = 20, hop: int = 5, rank: int = 3):
    inits, nodes = [], []
    if rank == 3:
        nodes.append(helper.make_node("Unsqueeze", ["x", "axes"], ["sig"]))
        inits.append(numpy_helper.from_array(np.array([2], np.int64), "axes"))
    else:  # Kokoro feeds a rank-2 [batch, samples] signal
        nodes.append(helper.make_node("Identity", ["x"], ["sig"]))
    if window_kind == "initializer":
        inits.append(numpy_helper.from_array(np.hanning(n).astype(np.float32), "win"))
    elif window_kind == "constant":
        nodes.append(helper.make_node("Constant", [], ["win"],
                                      value=numpy_helper.from_array(np.hamming(n).astype(np.float32))))
    elif window_kind == "hann_op":
        inits.append(numpy_helper.from_array(np.array(n, np.int64), "wsize"))
        nodes.append(helper.make_node("HannWindow", ["wsize"], ["win"], periodic=1))
    inits.append(numpy_helper.from_array(np.array(hop, np.int64), "step"))
    inits.append(numpy_helper.from_array(np.array(n, np.int64), "flen"))
    win = "" if window_kind == "none" else "win"
    nodes.append(helper.make_node("STFT", ["sig", "step", win, "flen"], ["y"], onesided=onesided))
    graph = helper.make_graph(nodes, "g",
                              [helper.make_tensor_value_info("x", TensorProto.FLOAT, [1, None])],
                              [helper.make_tensor_value_info("y", TensorProto.FLOAT, [None, None, None, None])], inits)
    return helper.make_model(graph, opset_imports=[helper.make_opsetid("", 17)], ir_version=9)


def run(model, x):
    sess = ort.InferenceSession(model.SerializeToString(), providers=["CPUExecutionProvider"])
    return sess.run(None, {"x": x})[0]


@pytest.mark.parametrize("window_kind", ["initializer", "constant", "hann_op", "none"])
@pytest.mark.parametrize("onesided", [1, 0])
@pytest.mark.parametrize("rank", [3, 2])
def test_conv_matches_stft(window_kind, onesided, rank):
    x = np.random.default_rng(0).standard_normal((1, 403)).astype(np.float32)
    model = stft_model(window_kind, onesided, rank=rank)
    try:
        want = run(model, x)
    except Exception:
        pytest.skip("this onnxruntime build rejects rank-2 STFT input")
    report = replace_stft(model)
    onnx.checker.check_model(model)
    assert "-> Conv" in report[0]
    assert not any(n.op_type == "STFT" for n in model.graph.node)
    got = run(model, x)
    # Loading also runs onnxruntime's shape inference, which is what failed on the owner's PC.
    assert got.shape == want.shape
    np.testing.assert_allclose(got, want, atol=2e-4, rtol=1e-4)


def test_dynamic_window_left_alone():
    model = stft_model("initializer")
    # make the frame step dynamic (a graph input) -> can't convert
    model.graph.initializer.remove(next(i for i in model.graph.initializer if i.name == "step"))
    model.graph.input.append(helper.make_tensor_value_info("step", TensorProto.INT64, []))
    report = replace_stft(model)
    assert "left on CPU" in report[0]
    assert any(n.op_type == "STFT" for n in model.graph.node)


def test_math_check_with_kokoro_settings():
    from assistant.voice.tts.onnx_fix import stft_math_error, stft_specs
    model = stft_model("initializer", rank=2)          # frame 20, hop 5, like Kokoro
    (spec,) = stft_specs(model)
    assert (spec["frame_length"], spec["step"], spec["onesided"]) == (20, 5, True)
    assert stft_math_error(spec) < 1e-5


def test_unused_constants_pruned():
    model = stft_model("initializer", rank=2)
    replace_stft(model)
    names = {i.name for i in model.graph.initializer}
    assert not names & {"step", "flen", "win"}        # only the old STFT used these


def test_verification_rules():
    from assistant.voice.tts.onnx_fix import verification_ok
    good = {"stft_math_error": 1e-6, "reference_diff": 1e-5, "length_ratio": 1.0,
            "loudness_vs_original": 0.9, "loudness_vs_reference": 1.0}
    assert verification_ok(good)
    assert not verification_ok({**good, "stft_math_error": 1e-2})
    assert not verification_ok({**good, "reference_diff": 0.2})
    assert not verification_ok({**good, "length_ratio": 1.5})


@pytest.mark.parametrize("rank", [2, 3])
def test_edge_bins_follow_pytorch_phase(rank):
    """0 Hz / Nyquist imag must be a tiny *positive* value so the exported atan pattern gives
    +pi for negative values, like torch.angle on PyTorch's exact-zero imag."""
    from assistant.voice.tts.onnx_fix import PHASE_EPS
    x = np.random.default_rng(3).standard_normal((1, 4003)).astype(np.float32)
    model = stft_model("initializer", rank=rank)
    replace_stft(model)
    got = run(model, x)
    assert np.all(got[..., 0, 1] == np.float32(PHASE_EPS))
    assert np.all(got[..., -1, 1] == np.float32(PHASE_EPS))
    # Phase via the exported atan pattern, vs numpy's exact FFT + angle (PyTorch semantics).
    re, im = got[..., 0].astype(np.float64), got[..., 1].astype(np.float64)
    atan = np.arctan(im / re)
    exported = np.where(re < 0, np.where(im > 0, atan + np.pi, atan - np.pi), atan)
    frames = np.lib.stride_tricks.sliding_window_view(x[0], 20)[::5] * np.hanning(20)
    spec = np.fft.rfft(frames, axis=-1)
    ok = np.abs(spec) > 1e-3
    diff = np.angle(np.exp(1j * (exported[0] - np.angle(spec))))
    assert np.max(np.abs(diff[ok])) < 1e-3
    assert np.all(exported[0][:, 0][re[0][:, 0] < 0] > 3.14)   # +pi, not -pi


def test_reference_model_masks_edge_bins(tmp_path):
    from assistant.voice.tts.onnx_fix import PHASE_EPS, make_reference
    src, ref = tmp_path / "m.onnx", tmp_path / "r.onnx"
    onnx.save(stft_model("initializer", rank=2), str(src))
    make_reference(src, ref)
    x = np.random.default_rng(4).standard_normal((1, 999)).astype(np.float32)
    sess = ort.InferenceSession(str(ref), providers=["CPUExecutionProvider"])
    y = sess.run(None, {"x": x})[0]
    assert np.all(y[..., 0, 1] == np.float32(PHASE_EPS)) and np.all(y[..., -1, 1] == np.float32(PHASE_EPS))
    conv = stft_model("initializer", rank=2)
    replace_stft(conv)
    np.testing.assert_allclose(run(conv, x), y, atol=2e-4, rtol=1e-4)
