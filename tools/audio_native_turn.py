#!/usr/bin/env python
"""Audio-native turn detection: does MEANING read from the waveform beat prosody?

The question this answers
-------------------------
`telugu_turn.extract_features` describes a turn with 16 hand-built numbers:
energy slopes, f0 slopes, voicing fraction, spectral centroid. Pure prosody --
HOW the voice moved, never WHAT was said. Measured 7 Sep against a stopwatch
control (`tools/sweep_timer_control.py`, docs 30), the entire prosody programme
is worth about five percentage points of cut-off rate.

Prosody cannot separate these two, and that is the whole problem:

    "naaku kaavaali..."   (I want...)   NOT finished
    "avunu"               (yes)         finished

Both trail off. Both fall in pitch and energy. To an ear that hears only the
shape of the sound they are identical; only MEANING tells them apart.

Why audio-native, and not a transcript
--------------------------------------
The obvious fix is to read the transcript. It is the wrong fix here:

  - production runs `saarika:v2.5`, which emits no interim transcripts at all,
    so no text exists until the caller has already stopped
  - text costs 0.37s, which is most of the endpoint budget
  - an LLM in the turn path was tried once and reverted (run 792, four defects)
  - while the BOT is speaking there is no transcript at all, so backchannel
    detection -- "aa" said over us -- could never work from text

So the semantics have to come from the AUDIO. That is what the 2026 literature
converged on and what the vendor models do:

  Smart Turn v3   Whisper-tiny ENCODER -> linear head. 8M params, 12ms CPU.
                  23 languages, no Telugu. Measured here: does not transfer
                  (docs 30) -- on Telugu it behaves as a 2.2s stopwatch.
  FastTurn        Conformer encoder + streaming CTC + LLM adapter, ~120ms.
  Easy Turn       acoustic + linguistic -> complete / incomplete /
                  backchannel / wait. The four states this project needs.

The common substrate is a pretrained speech encoder. Whisper's encoder saw
680k hours of multilingual audio including Telugu, so its representation
already carries phonetic and lexical structure that 16 hand-built numbers
cannot reach. Smart Turn v3 fails on Telugu not because the encoder is wrong
but because its HEAD was trained on 23 other languages.

We hold exactly what is missing: 2,950 real Telugu bursts from 647 real calls,
1,707 genuine turn ends and 1,243 genuine mid-thought pauses
(`.tmp/harvest/turnstops_real.jsonl`), every one with its audio on disk.

So: keep the encoder, retrain the head on Telugu.

Windowing matches pipecat exactly, so a head trained here stays compatible with
`LocalSmartTurnAnalyzerV3` and with the bundled ONNX: the last 8s ending at the
decision point, resampled to 16 kHz, right zero-padded to 128,000 samples,
80 x 800 log-mel (`_whisper_features.py`).

    python tools/audio_native_turn.py extract [--limit N]
    python tools/audio_native_turn.py train
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import wave
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
DOGRAH = REPO.parent / "dograh-vapi"
sys.path.insert(0, str(DOGRAH / "pipecat" / "src"))

LABELS = REPO / ".tmp" / "harvest" / "turnstops_real.jsonl"
CALLER = REPO / ".tmp" / "audio" / "caller"
CACHE = REPO / ".tmp" / "audio_native"
WINDOW_S = 8.0
MODEL_SR = 16000

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")


def _rows() -> list[dict]:
    return [json.loads(line) for line in LABELS.open(encoding="utf-8")]


def _load_encoder():
    """Whisper-tiny's encoder, with its positional table cut to an 8s window.

    HF's `WhisperEncoder` hard-checks for 3000 mel frames (30s) because that is
    what Whisper transcribes. Turn-taking does not need 30s of context and
    cannot afford it, so smart-turn-v3 truncates the learned positional
    embeddings rather than padding the audio. Two conv layers with a combined
    stride of 2 turn 800 mel frames into 400 encoder positions.
    """
    import torch
    from transformers import WhisperModel

    enc = WhisperModel.from_pretrained("openai/whisper-tiny").encoder.eval()
    keep = int(WINDOW_S * 100) // 2                      # 800 mel -> 400 pos
    with torch.no_grad():
        w = enc.embed_positions.weight[:keep].clone()
    enc.embed_positions = torch.nn.Embedding(keep, w.shape[1])
    with torch.no_grad():
        enc.embed_positions.weight.copy_(w)
    enc.config.max_source_positions = keep
    for p in enc.parameters():
        p.requires_grad_(False)
    return enc


def _window(row: dict) -> np.ndarray | None:
    """The 8s of caller audio ENDING at this burst's decision point.

    Ending at, not centred on: the model is asked "is he done NOW", so the last
    moment before the pause is the one carrying the answer. Anything after it
    is information the live system would not have had, and training on it would
    flatter the score by exactly the amount that matters.
    """
    p = CALLER / f"wf{row['workflow']}_run{row['run']}.wav"
    if not p.exists():
        return None
    with wave.open(str(p)) as w:
        sr, pcm = w.getframerate(), w.readframes(w.getnframes())
    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    end = int(float(row["end"]) * sr)
    seg = x[max(0, end - int(WINDOW_S * sr)):end]
    if seg.size < sr // 10:                              # under 100ms: nothing to read
        return None
    import soxr
    return soxr.resample(seg, sr, MODEL_SR).astype(np.float32)


def extract(limit: int | None) -> int:
    """Whisper encoder embeddings for every labelled burst, cached to disk."""
    import torch
    from pipecat.audio.turn.smart_turn._whisper_features import (
        compute_whisper_log_mel_features)

    rows = _rows()[:limit] if limit else _rows()
    enc = _load_encoder()
    print(f"encoder ready (d_model={enc.config.d_model}, "
          f"{enc.config.encoder_layers} layers); {len(rows)} bursts")

    feats, labels, calls, mels = [], [], [], []
    t0 = time.time()
    for i, r in enumerate(rows):
        seg = _window(r)
        if seg is None:
            continue
        mel = compute_whisper_log_mel_features(seg)
        mels.append(mel.astype(np.float16))     # kept, so the encoder can be tuned
        with torch.no_grad():
            h = enc(torch.from_numpy(mel)[None]).last_hidden_state[0].numpy()
        # Mean over the whole window AND over the tail. Turn-taking evidence is
        # concentrated at the END -- the last few hundred ms carry the falling
        # or suspended contour -- but the whole window carries what was said.
        # Keeping both lets the head weigh them. Keeping only the mean would
        # throw away the moment the decision is actually about.
        emb = np.concatenate([h.mean(0), h[-50:].mean(0), h[-10:].mean(0)])
        feats.append(emb.astype(np.float32))
        labels.append(1 if str(r["was_turn_end"]).lower() == "true" else 0)
        calls.append(f"{r['workflow']}_{r['run']}")
        if (i + 1) % 200 == 0:
            rate = (i + 1) / (time.time() - t0)
            print(f"  {i+1}/{len(rows)}  {rate:.1f}/s  "
                  f"eta {(len(rows)-i-1)/rate/60:.1f}m", flush=True)

    CACHE.mkdir(parents=True, exist_ok=True)
    out = CACHE / "whisper_tiny_enc.npz"
    np.savez_compressed(out, X=np.stack(feats), y=np.array(labels),
                        calls=np.array(calls))
    # The mel inputs, kept so the encoder can be FINE-TUNED and not only
    # probed. A frozen encoder with a linear head measures whether the signal
    # is present; training THROUGH the encoder is what actually reads it, and
    # is how smart-turn-v3 itself was built.
    mout = CACHE / "mels.npy"
    np.save(mout, np.stack(mels))
    print(f"  mels -> {mout}  ({mout.stat().st_size/1e6:.0f} MB)")
    print(f"\n{len(feats)} bursts -> {out}  ({out.stat().st_size/1e6:.1f} MB)")
    print(f"  complete={int(np.sum(labels))}  "
          f"incomplete={len(labels)-int(np.sum(labels))}")
    print(f"  took {(time.time()-t0)/60:.1f}m")
    return 0


def extract_prosody(limit: int | None) -> int:
    """The SAME 16 hand-built features, on the SAME windows, for a fair fight.

    docs 29 quotes 6.45% / 12.38% for the shipped forest, but on a holdout built
    by a different tool at a different time. Quoting a baseline across harnesses
    is how two honest numbers end up disagreeing. This recomputes the
    incumbent's own features here, so both models are scored on one split, from
    one file, in one run, and any difference is the FEATURES and nothing else.
    """
    sys.path.insert(0, str(DOGRAH))
    from api.services.vaani.telugu_turn import extract_features

    rows = _rows()[:limit] if limit else _rows()
    feats, labels, calls = [], [], []
    for r in rows:
        seg = _window(r)
        if seg is None:
            continue
        f = extract_features(seg, MODEL_SR)
        if f is None:
            continue
        feats.append(np.asarray(f, dtype=np.float32))
        labels.append(1 if str(r["was_turn_end"]).lower() == "true" else 0)
        calls.append(f"{r['workflow']}_{r['run']}")

    CACHE.mkdir(parents=True, exist_ok=True)
    out = CACHE / "prosody16.npz"
    np.savez_compressed(out, X=np.stack(feats), y=np.array(labels),
                        calls=np.array(calls))
    print(f"{len(feats)} bursts -> {out}")
    return 0


def _by_call_split(calls: np.ndarray, seed: int = 0, frac: float = 0.25):
    """Split by CALL, never by burst.

    Two bursts from one call share a speaker, a handset and a noise floor. Put
    them on opposite sides of the fence and the score measures voice
    recognition, not turn detection. `score_turn_models.py` splits this way and
    so does this, so the numbers are comparable.
    """
    uniq = np.array(sorted(set(calls.tolist())))
    rng = np.random.default_rng(seed)
    rng.shuffle(uniq)
    held = set(uniq[:max(1, int(len(uniq) * frac))].tolist())
    test = np.array([c in held for c in calls])
    return ~test, test


def _fit_score(X, y, tr, te, C):
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler
    sc = StandardScaler().fit(X[tr])
    clf = LogisticRegression(max_iter=3000, C=C).fit(sc.transform(X[tr]), y[tr])
    return clf.predict_proba(sc.transform(X[te]))[:, 1], y[te]


def _curve(p, yt):
    """Endable-early at every false-cutoff bar. The only two rates that count.

    false cutoff  = we said COMPLETE and the caller was still talking
    endable early = we said COMPLETE and he really had finished

    Never accuracy. A model with better accuracy and a worse cut-off rate is a
    WORSE model, and promoting it on accuracy is the classic way to ship a
    regression -- the rule `tools/model_registry.py` already enforces.
    """
    out = []
    for thr in np.arange(0.05, 0.999, 0.005):
        said = p >= thr
        fc = float(np.mean(said[yt == 0])) if (yt == 0).any() else 0.0
        ee = float(np.mean(said[yt == 1])) if (yt == 1).any() else 0.0
        out.append((float(thr), fc, ee))
    return out


def _best_under(curve, bar):
    ok = [c for c in curve if c[1] <= bar]
    return max(ok, key=lambda c: c[2]) if ok else None


def train() -> int:
    dw = np.load(CACHE / "whisper_tiny_enc.npz")
    dp = np.load(CACHE / "prosody16.npz")
    assert (dw["calls"] == dp["calls"]).all(), "the two caches disagree on rows"
    assert (dw["y"] == dp["y"]).all(), "the two caches disagree on labels"

    X, y, calls = dw["X"], dw["y"], dw["calls"]
    tr, te = _by_call_split(calls)
    print(f"{len(y)} bursts, {len(set(calls.tolist()))} calls")
    print(f"train {tr.sum()} / test {te.sum()} bursts, split by CALL")
    print(f"whisper-encoder dims {X.shape[1]}   prosody dims {dp['X'].shape[1]}\n")

    pw, yt = _fit_score(X, y, tr, te, C=0.05)
    pp, _ = _fit_score(dp["X"], y, tr, te, C=1.0)
    cw, cp = _curve(pw, yt), _curve(pp, yt)

    print("How much waiting each model removes, at the same safety bar.")
    print("(endable early = share of finished turns it can end sooner)\n")
    print(f"{'false-cutoff bar':>18} {'PROSODY 16':>13} {'AUDIO-NATIVE':>14} {'gain':>10}")
    print("-" * 60)
    for bar in (0.01, 0.02, 0.03, 0.05, 0.10):
        bp, bw = _best_under(cp, bar), _best_under(cw, bar)
        sp = f"{bp[2]*100:.1f}%" if bp else "--"
        sw = f"{bw[2]*100:.1f}%" if bw else "--"
        gain = f"{(bw[2]-bp[2])*100:+.1f} pts" if (bp and bw) else "--"
        print(f"{bar*100:>16.0f}% {sp:>13} {sw:>14} {gain:>10}")

    from sklearn.metrics import roc_auc_score
    aw, ap_ = roc_auc_score(yt, pw), roc_auc_score(yt, pp)
    print(f"\n  AUC   prosody {ap_:.3f}    audio-native {aw:.3f}    (0.5 = coin flip)")
    print("\nThe declared safety bar is 2% (latency_budget.yaml,")
    print("max_false_interruption_rate). Read that row first.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("extract")
    e.add_argument("--limit", type=int)
    q = sub.add_parser("extract-prosody")
    q.add_argument("--limit", type=int)
    sub.add_parser("train")
    a = ap.parse_args()
    if a.cmd == "extract":
        return extract(a.limit)
    if a.cmd == "extract-prosody":
        return extract_prosody(a.limit)
    return train()


if __name__ == "__main__":
    raise SystemExit(main())
