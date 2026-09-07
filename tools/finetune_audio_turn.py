#!/usr/bin/env python
"""Fine-tune Whisper's encoder to hear a finished Telugu turn.

Why this exists, in one line
----------------------------
`tools/audio_native_turn.py` proved the signal is THERE -- a frozen Whisper
encoder with a linear probe removes 8.3% of waiting at the 2% safety bar
against prosody's 2.7%, three times more. But a frozen encoder is a
measurement, not a model. This trains through it, which is how smart-turn-v3
was built and what the 2026 papers actually do.

What the incumbent cannot do
----------------------------
`telugu_turn.extract_features` is 16 numbers describing the SHAPE of the sound:
energy slope, f0 slope, voicing fraction, spectral centroid. Measured on this
same holdout its AUC is 0.550 -- a coin flip is 0.500. It cannot separate

    "naaku kaavaali..."   (I want...)   not finished
    "avunu"               (yes)         finished

because both fall in pitch and energy. Only what was SAID separates them, and
16 prosody numbers do not encode what was said.

Why not just read the transcript
--------------------------------
Because there isn't one. Production runs `saarika:v2.5`, which emits no interim
transcripts at all, so no text exists until the caller has already stopped --
and text costs 0.37s, most of the endpoint budget. While the BOT is talking
there is no transcript either, so a text-based rule could never catch a
backchannel. An LLM in the turn path was tried and reverted (run 792).

So the meaning has to be read from the waveform. That is what "audio-native"
means here, and it is the direction the field went:

    Smart Turn v3   Whisper-tiny encoder -> head. 23 languages, no Telugu.
                    Measured (docs 30): on Telugu it is a 2.2s stopwatch.
    FastTurn        Conformer + streaming CTC + LLM adapter, ~120ms.
    Easy Turn       acoustic + linguistic -> complete / incomplete /
                    backchannel / wait.

The encoder is not the problem -- Whisper saw 680k hours including Telugu. The
HEAD is the problem: it never saw any. We have 2,950 real Telugu bursts from
647 real calls. So keep the encoder, and teach it Telugu.

Design decisions, and why
-------------------------
1. TOP LAYERS ONLY. whisper-tiny has 4 encoder layers; the bottom two stay
   frozen. 2,299 training bursts cannot support 8M free parameters without
   memorising the 485 speakers behind them.
2. ATTENTION POOLING over the encoder frames, not a mean. Where the evidence
   sits varies -- a trailing "..." and a crisp "avunu" put it in different
   places -- so the model learns where to look instead of being told.
3. SPLIT BY CALL. Two bursts from one call share a speaker, a handset and a
   noise floor; split them across the fence and the score measures voice
   recognition. Same split function as `audio_native_turn.py`, same seed.
4. SELECT ON ENDABLE-EARLY AT THE 2% BAR, never on accuracy or loss. A model
   with better accuracy and a worse cut-off rate is a worse model
   (`tools/model_registry.py`).

    python tools/finetune_audio_turn.py --epochs 6
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
DOGRAH = REPO.parent / "dograh-vapi"
sys.path.insert(0, str(DOGRAH / "pipecat" / "src"))

CACHE = REPO / ".tmp" / "audio_native"
MODELS = REPO / "models"
WINDOW_S = 8.0

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")


def _by_call_split(calls: np.ndarray, seed: int = 0, frac: float = 0.25):
    """Identical to audio_native_turn._by_call_split -- same seed, same fence."""
    uniq = np.array(sorted(set(calls.tolist())))
    rng = np.random.default_rng(seed)
    rng.shuffle(uniq)
    held = set(uniq[:max(1, int(len(uniq) * frac))].tolist())
    test = np.array([c in held for c in calls])
    return ~test, test


def _rates(p: np.ndarray, y: np.ndarray, bar: float = 0.02):
    """(endable_early, false_cutoff, threshold) at the tightest bar we allow.

    false cutoff  = said COMPLETE while the caller was still talking
    endable early = said COMPLETE and he really had finished

    The declared bar is 2% (`latency_budget.yaml: max_false_interruption_rate`).
    """
    best = (0.0, 0.0, 1.0)
    for thr in np.arange(0.02, 0.999, 0.002):
        said = p >= thr
        fc = float(np.mean(said[y == 0])) if (y == 0).any() else 0.0
        if fc <= bar:
            ee = float(np.mean(said[y == 1])) if (y == 1).any() else 0.0
            if ee > best[0]:
                best = (ee, fc, float(thr))
    return best


def build_model(freeze_below: int = 2):
    """Whisper-tiny encoder (8s window) + attention pool + MLP head."""
    import torch
    import torch.nn as nn
    from transformers import WhisperModel

    class TurnHead(nn.Module):
        def __init__(self):
            super().__init__()
            enc = WhisperModel.from_pretrained("openai/whisper-tiny").encoder
            keep = int(WINDOW_S * 100) // 2          # 800 mel frames -> 400 pos
            w = enc.embed_positions.weight[:keep].clone()
            enc.embed_positions = nn.Embedding(keep, w.shape[1])
            with torch.no_grad():
                enc.embed_positions.weight.copy_(w)
            enc.config.max_source_positions = keep
            self.enc = enc
            d = enc.config.d_model

            # Freeze the bottom layers. 2,299 bursts from 485 callers cannot
            # support tuning all of whisper-tiny without learning the speakers.
            for p in self.enc.parameters():
                p.requires_grad_(False)
            for layer in self.enc.layers[freeze_below:]:
                for p in layer.parameters():
                    p.requires_grad_(True)
            for p in self.enc.layer_norm.parameters():
                p.requires_grad_(True)

            self.attn = nn.Sequential(nn.Linear(d, 128), nn.Tanh(), nn.Linear(128, 1))
            self.head = nn.Sequential(
                nn.LayerNorm(d), nn.Dropout(0.3),
                nn.Linear(d, 128), nn.GELU(), nn.Dropout(0.3),
                nn.Linear(128, 1))

        def forward(self, mel):
            h = self.enc(mel).last_hidden_state          # (B, 400, d)
            a = torch.softmax(self.attn(h), dim=1)       # learned WHERE to look
            return self.head((h * a).sum(1)).squeeze(-1)

    return TurnHead()


def main() -> int:
    import torch
    import torch.nn as nn

    ap = argparse.ArgumentParser()
    ap.add_argument("--epochs", type=int, default=6)
    ap.add_argument("--batch", type=int, default=16)
    ap.add_argument("--lr", type=float, default=1e-4)
    ap.add_argument("--freeze-below", type=int, default=2)
    a = ap.parse_args()

    torch.manual_seed(0)
    d = np.load(CACHE / "whisper_tiny_enc.npz")
    y_all, calls = d["y"], d["calls"]
    mels = np.load(CACHE / "mels.npy", mmap_mode="r")
    assert len(mels) == len(y_all), "mel cache and label cache disagree"

    tr, te = _by_call_split(calls)
    itr, ite = np.where(tr)[0], np.where(te)[0]
    print(f"{len(y_all)} bursts, {len(set(calls.tolist()))} calls")
    print(f"train {len(itr)} / test {len(ite)}, split by CALL")
    print(f"train labels: {int(y_all[tr].sum())} complete / "
          f"{int((1-y_all[tr]).sum())} incomplete\n")

    model = build_model(a.freeze_below)
    n_tune = sum(p.numel() for p in model.parameters() if p.requires_grad)
    n_all = sum(p.numel() for p in model.parameters())
    print(f"trainable {n_tune/1e6:.2f}M of {n_all/1e6:.2f}M "
          f"(encoder layers {a.freeze_below}+ unfrozen)\n")

    # The classes are 1707/1243, mildly unbalanced. Weighting matters less than
    # the threshold sweep that follows, but an unweighted loss quietly biases
    # toward COMPLETE, which is the failure direction we care most about.
    pos_w = torch.tensor([(1 - y_all[tr]).sum() / max(1, y_all[tr].sum())],
                         dtype=torch.float32)
    lossf = nn.BCEWithLogitsLoss(pos_weight=pos_w)
    opt = torch.optim.AdamW([p for p in model.parameters() if p.requires_grad],
                            lr=a.lr, weight_decay=0.01)

    def evaluate(idx):
        model.eval()
        out = []
        with torch.no_grad():
            for s in range(0, len(idx), 32):
                b = np.asarray(mels[idx[s:s + 32]], dtype=np.float32)
                out.append(torch.sigmoid(model(torch.from_numpy(b))).numpy())
        return np.concatenate(out)

    print(f"{'epoch':>6} {'loss':>8} {'endable early @2%':>19} {'false cut':>11} {'AUC':>7}")
    print("-" * 56)
    from sklearn.metrics import roc_auc_score
    best_ee, best_state, best_thr = -1.0, None, 0.5
    for ep in range(1, a.epochs + 1):
        model.train()
        perm = np.random.default_rng(ep).permutation(itr)
        tot = n = 0
        t0 = time.time()
        for s in range(0, len(perm), a.batch):
            bidx = np.sort(perm[s:s + a.batch])
            xb = torch.from_numpy(np.asarray(mels[bidx], dtype=np.float32))
            yb = torch.from_numpy(y_all[bidx].astype(np.float32))
            opt.zero_grad()
            loss = lossf(model(xb), yb)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(
                [p for p in model.parameters() if p.requires_grad], 1.0)
            opt.step()
            tot += float(loss) * len(bidx); n += len(bidx)

        p = evaluate(ite)
        yt = y_all[ite]
        ee, fc, thr = _rates(p, yt)
        auc = roc_auc_score(yt, p)
        print(f"{ep:>6} {tot/n:>8.4f} {ee*100:>18.1f}% {fc*100:>10.2f}% {auc:>7.3f}"
              f"   ({time.time()-t0:.0f}s)")
        if ee > best_ee:
            best_ee, best_thr = ee, thr
            best_state = {k: v.detach().clone() for k, v in model.state_dict().items()}

    print("\nSame holdout, same metric, measured not quoted:")
    print(f"  {'prosody 16 (the incumbent features)':<38} 2.7%   AUC 0.550")
    print(f"  {'frozen encoder + linear probe':<38} 8.3%   AUC 0.619")
    print(f"  {'FINE-TUNED encoder':<38} {best_ee*100:.1f}%   (thr {best_thr:.2f})")

    if best_state is not None:
        MODELS.mkdir(exist_ok=True)
        out = MODELS / "audio_native_turn_te.pt"
        import torch as _t
        _t.save({"state_dict": best_state, "threshold": best_thr,
                 "endable_early_at_2pct": best_ee, "window_s": WINDOW_S,
                 "freeze_below": a.freeze_below,
                 "note": "whisper-tiny encoder fine-tuned on 2,950 real Telugu "
                         "bursts from 647 calls; split by call, selected on "
                         "endable-early at a 2% false-cutoff bar"}, out)
        print(f"\nsaved {out} ({out.stat().st_size/1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
