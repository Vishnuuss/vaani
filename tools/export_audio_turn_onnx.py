#!/usr/bin/env python
"""Export the Telugu turn head to ONNX, because 95 ms sits on every turn.

The number this exists to remove
---------------------------------
`AudioNativeTurnAnalyzer` reads the turn from the waveform and beats the 16
prosody features 4x on the declared safety bar. In PyTorch it costs **95 ms per
decision**, measured, and the endpoint is consulted more than once on a turn
where the caller pauses mid-thought -- exactly the turns this model exists to
get right. Against an 800 ms budget, and a target of 600 ms, that is not a
rounding error.

Smart Turn v3 ships the same shape of model as int8 ONNX and reports 12 ms on
CPU. There is no reason ours should be slower, and the arithmetic is the whole
argument: at 95 ms a three-probe turn spends 285 ms deciding whether the caller
stopped. At 12 ms it spends 36 ms.

Correctness before speed
------------------------
An export that changes the verdict is not an optimisation, it is a new model
with no measurements. So this checks agreement against the PyTorch original on
real cached mel windows and refuses to write a graph that disagrees.

    python tools/export_audio_turn_onnx.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
DOGRAH = REPO.parent / "dograh-vapi"
sys.path.insert(0, str(DOGRAH / "pipecat" / "src"))
sys.path.insert(0, str(DOGRAH))

SRC = DOGRAH / "api" / "services" / "vaani" / "models" / "audio_native_turn_te.pt"
DST = DOGRAH / "api" / "services" / "vaani" / "models" / "audio_native_turn_te.onnx"
MELS = REPO / ".tmp" / "audio_native" / "mels.npy"

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")


def main() -> int:
    import torch
    from api.services.vaani.audio_native_turn import _build_model

    blob = torch.load(SRC, map_location="cpu", weights_only=True)
    model = _build_model(int(blob.get("freeze_below", 3)))
    model.load_state_dict(blob["state_dict"])
    model.eval()
    thr = float(blob.get("threshold", 0.94))
    print(f"loaded {SRC.name}  threshold {thr:.3f}")

    dummy = torch.zeros(1, 80, 800)
    torch.onnx.export(
        model, dummy, str(DST),
        input_names=["mel"], output_names=["logit"],
        dynamic_axes={"mel": {0: "batch"}, "logit": {0: "batch"}},
        opset_version=17, do_constant_folding=True,
        # Legacy TorchScript exporter. torch 2.13's dynamo path needs
        # onnxscript, which is not installed here and is not worth a new
        # dependency for a graph this small.
        dynamo=False)
    print(f"exported -> {DST.name}  ({DST.stat().st_size/1e6:.1f} MB)")

    # --- agreement, on REAL windows rather than noise ----------------------
    import onnxruntime as ort
    sess = ort.InferenceSession(str(DST), providers=["CPUExecutionProvider"])

    if not MELS.exists():
        print("no cached mels; skipping the agreement check (RUN IT BEFORE SHIPPING)")
        return 1
    mels = np.load(MELS, mmap_mode="r")
    idx = np.linspace(0, len(mels) - 1, 60).astype(int)

    def sig(x):
        return 1.0 / (1.0 + np.exp(-x))

    pt, on = [], []
    t_pt = t_on = 0.0
    for i in idx:
        m = np.asarray(mels[i], dtype=np.float32)[None]
        t0 = time.perf_counter()
        with torch.no_grad():
            pt.append(float(torch.sigmoid(model(torch.from_numpy(m))).item()))
        t_pt += time.perf_counter() - t0
        t0 = time.perf_counter()
        on.append(float(sig(sess.run(None, {"mel": m})[0][0])))
        t_on += time.perf_counter() - t0

    pt, on = np.array(pt), np.array(on)
    max_delta = float(np.abs(pt - on).max())
    # The verdict, not the probability, is what reaches the caller.
    verdict_same = float(np.mean((pt >= thr) == (on >= thr)))
    print(f"\n  agreement on {len(idx)} real windows")
    print(f"    max |p_torch - p_onnx| : {max_delta:.6f}")
    print(f"    identical verdicts     : {verdict_same*100:.1f}%")
    print(f"    torch {t_pt/len(idx)*1000:6.1f} ms/window")
    print(f"    onnx  {t_on/len(idx)*1000:6.1f} ms/window   "
          f"({t_pt/max(t_on,1e-9):.1f}x faster)")

    if max_delta > 1e-3 or verdict_same < 1.0:
        print("\nREFUSED: the export disagrees with the model that was measured. "
              "A graph that changes the verdict is a new model with no numbers "
              "behind it, not an optimisation.")
        DST.unlink(missing_ok=True)
        return 1
    print("\nverdicts identical -- safe to load in place of the .pt")

    # --- int8, which is where smart-turn-v3's 12 ms comes from -------------
    #
    # Quantisation is the only lever left worth pulling: fp32 ONNX is a few
    # times faster than torch and still tens of milliseconds, and the endpoint
    # is probed more than once on exactly the turns this model exists to get
    # right. int8 changes the arithmetic, so it is held to the same bar as the
    # export itself -- identical VERDICTS on real windows, or it is discarded.
    try:
        from onnxruntime.quantization import QuantType, quantize_dynamic
    except Exception as e:
        print(f"int8 skipped ({e.__class__.__name__}); fp32 stands")
        return 0

    q = DST.with_name(DST.stem + "_int8.onnx")
    quantize_dynamic(str(DST), str(q), weight_type=QuantType.QInt8)
    qs = ort.InferenceSession(str(q), providers=["CPUExecutionProvider"])
    q_p, t_q = [], 0.0
    for i in idx:
        m = np.asarray(mels[i], dtype=np.float32)[None]
        t0 = time.perf_counter()
        q_p.append(float(sig(qs.run(None, {"mel": m})[0][0])))
        t_q += time.perf_counter() - t0
    q_p = np.array(q_p)
    q_same = float(np.mean((pt >= thr) == (q_p >= thr)))
    print(f"\n  int8 ({q.stat().st_size/1e6:.1f} MB)")
    print(f"    identical verdicts : {q_same*100:.1f}%")
    print(f"    max delta          : {float(np.abs(pt - q_p).max()):.5f}")
    print(f"    {t_q/len(idx)*1000:6.1f} ms/window   "
          f"({t_pt/max(t_q,1e-9):.1f}x faster than torch)")
    if q_same < 1.0:
        q.unlink(missing_ok=True)
        print("    DISCARDED: int8 changes verdicts. fp32 stands.")
    else:
        print("    kept -- verdicts identical to the measured model")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
