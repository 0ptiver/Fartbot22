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
