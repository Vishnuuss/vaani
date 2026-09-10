"""Decide the turn from what was SAID, read straight off the waveform.

Why this exists
---------------
`telugu_turn.extract_features` describes a turn with 16 hand-built numbers:
energy slope, f0 slope, voicing fraction, spectral centroid. Prosody -- HOW the
voice moved, never WHAT was said. Measured on the real-label holdout its AUC is
**0.550**, against a coin flip's 0.500, and measured end to end against a
stopwatch control (docs 30) the whole prosody programme is worth about five
percentage points of cut-off rate.

It cannot separate the two cases the agent gets wrong all day, because they are
acoustically alike and differ only in meaning:

    "naaku kaavaali..."   (I want...)   NOT finished
    "avunu"               (yes)         finished

Reading a transcript is the wrong fix here. Production runs `saarika:v2.5`,
which emits no interim transcripts at all, so no text exists until the caller
has already stopped -- and while the BOT is speaking there is never any text, so
a backchannel could not be caught that way at all. An LLM in the turn path was
tried and reverted (run 792).

So the meaning is read from the audio, which is where the 2026 work went:
Smart Turn v3 (Whisper-tiny encoder + head), FastTurn (Conformer + CTC + LLM
adapter), Easy Turn (acoustic + linguistic -> complete/incomplete/backchannel/
wait). The substrate they share is a pretrained speech encoder.

Smart Turn v3 ships inside pipecat and does NOT transfer to Telugu -- measured,
docs 30: p50 == p90 == 2.18s, i.e. it almost never fires and is really a 2.2s
stopwatch, which a real stopwatch beats. The encoder is not the problem; its
HEAD only ever saw 23 other languages. So the encoder was kept and the head
retrained on 2,950 real Telugu bursts from 647 real calls
(`tools/finetune_audio_turn.py`), split by CALL, selected on endable-early at
the declared 2% false-cutoff bar:

    prosody 16 (what runs today)     2.7% endable early   AUC 0.550
    frozen encoder + linear probe    8.3%                 AUC 0.619
    FINE-TUNED encoder              10.8%                 AUC 0.670

What this file deliberately does NOT change
-------------------------------------------
The two-sided endpoint logic in `telugu_turn` -- the interpolated wait, the
fragment floor, the unsure band, and the per-caller adaptation that gets more
patient after it has cut somebody off twice -- is hard-won and is what stops the
agent talking over people. It is reused verbatim by subclassing. Only the
PROBABILITY is replaced. A better verdict feeding the same timers is the whole
change, and it keeps every regression those timers already pass.

Cost, and why inference is not per-frame
----------------------------------------
The encoder is ~8M parameters and costs roughly 130 ms per call on CPU in
PyTorch. That is affordable ONCE per endpoint decision and unaffordable every
20 ms frame, so it runs only after `min_silence_ms` of silence -- exactly where
`telugu_turn` already asks its own model. An ONNX export drops this to ~12 ms
and is the obvious next step, but it is an optimisation, not a prerequisite: the
model is consulted at most a handful of times per turn.
"""

from __future__ import annotations

import threading
from pathlib import Path

import numpy as np
from loguru import logger

from api.services.vaani.telugu_turn import TeluguTurnAnalyzer, TeluguTurnParams

MODEL_PATH = Path(__file__).parent / "models" / "audio_native_turn_te.pt"
WINDOW_S = 8.0
MODEL_SR = 16000

_LOCK = threading.Lock()
_RUNTIME = None            # (model, threshold) -- loaded once per process


def _build_model(freeze_below: int):
    """The same graph `tools/finetune_audio_turn.py` trained, rebuilt for load."""
    import torch
    import torch.nn as nn
    from transformers import WhisperModel

    class TurnHead(nn.Module):
        def __init__(self):
            super().__init__()
            enc = WhisperModel.from_pretrained("openai/whisper-tiny").encoder
            keep = int(WINDOW_S * 100) // 2       # 800 mel frames -> 400 positions
            w = enc.embed_positions.weight[:keep].clone()
            enc.embed_positions = nn.Embedding(keep, w.shape[1])
            with torch.no_grad():
                enc.embed_positions.weight.copy_(w)
            enc.config.max_source_positions = keep
            self.enc = enc
            d = enc.config.d_model
            self.attn = nn.Sequential(nn.Linear(d, 128), nn.Tanh(), nn.Linear(128, 1))
            self.head = nn.Sequential(
                nn.LayerNorm(d), nn.Dropout(0.3),
                nn.Linear(d, 128), nn.GELU(), nn.Dropout(0.3),
                nn.Linear(128, 1))

        def forward(self, mel):
            h = self.enc(mel).last_hidden_state
            a = torch.softmax(self.attn(h), dim=1)
            return self.head((h * a).sum(1)).squeeze(-1)

    return TurnHead()


class _Onnx:
    """Presents the ONNX session with the same call shape as the torch model.

    Kept deliberately thin: `_probability` should not care which runtime it
    got, so the branch lives here and nowhere on the call path.
    """

    def __init__(self, sess):
        self._sess = sess

    def probability(self, mel: np.ndarray) -> float:
        logit = self._sess.run(None, {"mel": mel[None].astype(np.float32)})[0]
        return float(1.0 / (1.0 + np.exp(-float(logit.reshape(-1)[0]))))


def _threshold_from_torch(pt_path: Path) -> float | None:
    """Read the threshold out of the .pt, for dev boxes with no sidecar.

    Returns None rather than a default when torch is absent or the file cannot
    be read. A GUESSED threshold is worse than no model: the agent would run,
    log nothing unusual, and be miscalibrated on every turn.
    """
    try:
        import torch
        blob = torch.load(pt_path, map_location="cpu", weights_only=True)
        return float(blob["threshold"])
    except Exception:
        return None


def _runtime(path: Path | None = None):
    """Load once per process. Never fatal -- a missing model degrades to prosody."""
    global _RUNTIME
    if _RUNTIME is not None:
        return _RUNTIME
    with _LOCK:
        if _RUNTIME is not None:
            return _RUNTIME
        # ONNX first. Measured on 60 real windows: 69 ms against torch's 148 ms,
        # with verdicts identical to five decimal places -- the export is
        # checked and refused by `tools/export_audio_turn_onnx.py` if it is not.
        # The endpoint is probed more than once on the turns this model exists
        # to get right, so halving that cost is worth the extra branch.
        #
        # int8 was tried and DISCARDED: 93.3% verdict agreement, and slower than
        # fp32 on this graph. Recorded so nobody spends the afternoon again.
        onnx_path = (path or MODEL_PATH).with_suffix(".onnx")
        if onnx_path.exists():
            try:
                # NO torch on this path, deliberately, and this is a deployment
                # fact rather than a preference. `api/Dockerfile` installs
                # pipecat WITHOUT the `local-smart-turn` extra, which is the
                # only thing that pulls in torch and transformers. `onnxruntime`
                # and `soxr` are pipecat BASE dependencies and are present.
                #
                # So on the server `import torch` raises. It used to sit right
                # here, purely to read one float out of the .pt, and it would
                # have thrown this whole branch into the fallback, then thrown
                # the fallback too, and left `_RUNTIME = (None, ...)`. The agent
                # would have run prosody while the logs said audio-native was
                # configured -- a silent no-op, which is the worst shape a
                # deployment bug can take.
                #
                # The threshold now travels beside the graph in a JSON sidecar
                # written by `tools/export_audio_turn_onnx.py`. It is a single
                # number; it does not need a tensor library to read it.
                import json

                import onnxruntime as ort

                meta_path = onnx_path.with_suffix(".json")
                meta = {}
                if meta_path.exists():
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                thr = meta.get("threshold")
                if thr is None:
                    # No sidecar: only possible on a dev box with an old export.
                    # Try torch, and if that is missing too, refuse rather than
                    # guess -- a wrong threshold is a miscalibrated agent, which
                    # is harder to notice than no agent at all.
                    thr = _threshold_from_torch(path or MODEL_PATH)
                if thr is None:
                    raise RuntimeError(
                        f"no threshold: {meta_path.name} is missing and torch "
                        "is unavailable to read it from the .pt")
                sess = ort.InferenceSession(str(onnx_path),
                                            providers=["CPUExecutionProvider"])
                logger.info(
                    f"[audio-native] ONNX runtime, threshold {float(thr):.3f}"
                    + (f", {meta.get('note')}" if meta.get("note") else ""))
                _RUNTIME = (_Onnx(sess), float(thr))
                return _RUNTIME
            except Exception as e:
                logger.warning(f"[audio-native] ONNX unusable ({e!r}); torch next")
        try:
            import torch
            # weights_only=True: this checkpoint holds tensors and plain
            # scalars only, so there is no reason to let a model file execute
            # arbitrary code on the call path.
            blob = torch.load(path or MODEL_PATH, map_location="cpu",
                              weights_only=True)
            model = _build_model(int(blob.get("freeze_below", 3)))
            model.load_state_dict(blob["state_dict"])
            model.eval()
            thr = float(blob.get("threshold", 0.94))
            logger.info(
                f"[audio-native] loaded {path or MODEL_PATH.name}, threshold "
                f"{thr:.2f} (measured: {blob.get('endable_early_at_2pct', 0)*100:.1f}% "
                "endable early at the 2% false-cutoff bar, against prosody's 2.7%)")
            _RUNTIME = (model, thr)
        except Exception as e:
            logger.warning(f"[audio-native] unavailable ({e!r}); prosody stands")
            _RUNTIME = (None, 1.1)
    return _RUNTIME


class AudioNativeTurnAnalyzer(TeluguTurnAnalyzer):
    """`TeluguTurnAnalyzer` with the 16 prosody numbers swapped for the encoder.

    Everything else -- the interpolated wait, the fragment floor, the unsure
    band, the per-caller patience after a cut-off -- is inherited unchanged,
    because that machinery is what stops the agent talking over people and it
    already passes every turn regression in the suite.
    """

    def __init__(self, *, sample_rate: int | None = None,
                 params: TeluguTurnParams | None = None,
                 model_path: Path | None = None, **kw):
        super().__init__(sample_rate=sample_rate, params=params, **kw)
        self._model, thr = _runtime(model_path)
        if self._model is not None:
            # The trained threshold, not the prosody one. `_bar()` and
            # `_wait_secs()` both read `params.threshold`, so the inherited
            # timers scale to THIS model's calibration rather than the forest's.
            self._params.threshold = thr
            self.enabled = True

    def _probability(self) -> float | None:
        """P(the caller has finished), read from the tail of his own audio."""
        if self._model is None:
            return super()._probability()
        try:
            # torch is imported ONLY in the torch branch below, never here.
            # The server has no torch -- see `_runtime` -- so importing it at
            # the top of this function would raise on EVERY scoring call, be
            # swallowed by the except, and silently run prosody on a config
            # that says audio-native. `soxr` and the mel helper are pipecat
            # base dependencies and are always present.
            import soxr
            from pipecat.audio.turn.smart_turn._whisper_features import (
                compute_whisper_log_mel_features)

            if not self._buffer:
                return None
            x = np.concatenate([a for _, a in self._buffer]).astype(np.float32)
            x = x / 32768.0
            sr = self._rate
            # The LAST 8 seconds: the model is asked "is he done NOW", so the
            # moment before the pause is the one carrying the answer. Same
            # window the head was trained on -- a different one here would score
            # nothing like the holdout.
            x = x[-int(WINDOW_S * sr):]
            if x.size < sr // 10:
                return None
            x16 = soxr.resample(x, sr, MODEL_SR).astype(np.float32)
            mel = compute_whisper_log_mel_features(x16)
            if isinstance(self._model, _Onnx):
                value = self._model.probability(mel)
            else:
                import torch
                with torch.no_grad():
                    value = float(torch.sigmoid(
                        self._model(torch.from_numpy(mel)[None])).item())
            self._last_probability = value
            return value
        except Exception as e:
            # Never fatal on a live call. Falling back to prosody is worse than
            # this model and far better than a turn that never ends.
            logger.warning(f"[audio-native] scoring failed ({e!r}); using prosody")
            return super()._probability()
