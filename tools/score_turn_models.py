#!/usr/bin/env python
"""Score ANY turn-detector artifact against the REAL labels, on one holdout.

Why this exists
---------------
`models/audio_turn_real_gbm.json` records its own false-cutoff and early-end
rates; the DEPLOYED model
(`dograh-vapi/api/services/vaani/models/audio_turn_gbm.json`) records neither,
because it was trained on `turnstops.jsonl` — 2,754 rows, all labelled
`was_turn_end: True`. There were no negatives to score against, so its cut-off
rate has never been measured at all.

"Is the new model better than the one running" therefore had no answer, because
half the comparison did not exist. This computes both sides on the SAME holdout,
with the SAME by-call split, so the two numbers are comparable.

The metric is the one `tools/model_registry.py` already declares, not accuracy:

    false cutoffs      the caller was still talking and we ended his turn.
                       THE cardinal failure. Must stay under 2%.
    endable early      how much waiting the model removes. The reward.

A model with better accuracy and a worse cutoff rate is a WORSE model.

    python tools/score_turn_models.py
"""
from __future__ import annotations

import json
import sys
import wave
from pathlib import Path

import numpy as np

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

REPO = Path(__file__).resolve().parents[1]
VAANI = REPO.parent / "dograh-vapi" / "api" / "services" / "vaani"
sys.path.insert(0, str(REPO.parent / "dograh-vapi"))


def _import_telugu_turn():
    """Import telugu_turn WITHOUT executing `api.services.vaani.__init__`.

    The package __init__ imports brain_processor -> reply_sanitizer, so a
    half-finished edit anywhere in that chain takes this tool down with it. That
    is not hypothetical: on 7 Sep a literal newline inside a string literal in
    reply_sanitizer.py made every `import api.services.vaani.*` a SyntaxError,
    and this script had run clean fifteen minutes earlier.

    A scoring tool has no business depending on the reply path. Registering a
    namespace package stub for `api.services.vaani` lets Python resolve
    `telugu_turn` and its `completeness` import by file, and leaves the rest of
    the package unexecuted -- so measurement stays possible while somebody else
    is editing the conversation code.
    """
    import importlib
    import types

    for name, path in (("api", VAANI.parents[1]),
                       ("api.services", VAANI.parent),
                       ("api.services.vaani", VAANI)):
        if name not in sys.modules:
            mod = types.ModuleType(name)
            mod.__path__ = [str(path)]          # a namespace package, no __init__
            sys.modules[name] = mod
    return importlib.import_module("api.services.vaani.telugu_turn")


_tt = _import_telugu_turn()
WINDOW_S, extract_features = _tt.WINDOW_S, _tt.extract_features

DATA = REPO / ".tmp" / "harvest" / "turnstops_real.jsonl"
CALLER = REPO / ".tmp" / "audio" / "caller"

SHIPPED = REPO.parent / "dograh-vapi" / "api" / "services" / "vaani" / "models" / "audio_turn_gbm.json"
RETRAINED = REPO / "models" / "audio_turn_real_gbm.json"
RETRAINED_LINEAR = REPO / "models" / "audio_turn_real.json"


def forest_probability(gbm: dict, x: np.ndarray) -> float:
    """Walk the exported trees. Mirrors TeluguTurnAnalyzer._Forest exactly."""
    total = float(gbm["init"])
    lr = float(gbm["learning_rate"])
    for t in gbm["trees"]:
        feat, thr = t["feature"], t["threshold"]
        left, right, val = t["left"], t["right"], t["value"]
        node = 0
        while left[node] != -1:
            node = left[node] if x[feat[node]] <= thr[node] else right[node]
        total += lr * val[node]
    return float(1.0 / (1.0 + np.exp(-total)))


def linear_probability(m: dict, x: np.ndarray) -> float:
    z = (x - np.asarray(m["mean"])) / np.asarray(m["scale"])
    return float(1.0 / (1.0 + np.exp(-(float(z @ np.asarray(m["coef"])) + m["intercept"]))))


def load_features():
    """Same extraction and the SAME by-call split as train_real_turn_detector."""
    rows = [json.loads(l) for l in DATA.open(encoding="utf-8")]
    by_call: dict[str, list] = {}
    for r in rows:
        by_call.setdefault(f"wf{r['workflow']}_run{r['run']}", []).append(r)

    X, y, groups = [], [], []
    for stem, rs in by_call.items():
        wav = CALLER / f"{stem}.wav"
        if not wav.exists():
            continue
        with wave.open(str(wav)) as w:
            sr = w.getframerate()
            pcm = w.readframes(w.getnframes())
        x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        for r in rs:
            hi = int(r["end"] * sr)
            lo = max(0, hi - int(WINDOW_S * sr))
            seg = x[lo:hi]
            if seg.size < int(0.2 * sr):
                continue
            f = extract_features(seg, sr)
            if f is None:
                continue
            X.append(f)
            y.append(1 if r["was_turn_end"] else 0)
            groups.append(stem)
    return np.asarray(X, float), np.asarray(y, int), np.asarray(groups)


def holdout(X, y, g, frac=0.25):
    calls = sorted(set(g.tolist()))
    rng = np.random.default_rng(0)
    rng.shuffle(calls)
    train = set(calls[: int(len(calls) * (1 - frac))])
    mask = np.array([c in train for c in g])
    return X[~mask], y[~mask]


def report(name: str, probs: np.ndarray, y: np.ndarray, own_threshold: float | None):
    neg, pos = (y == 0), (y == 1)
    print(f"\n=== {name} ===")
    if own_threshold is not None:
        fires = probs >= own_threshold
        co = (fires & neg).sum() / max(1, neg.sum())
        ea = (fires & pos).sum() / max(1, pos.sum())
        verdict = "OK" if co <= 0.02 else "FAILS the 2% bar"
        print(f"  at its OWN threshold {own_threshold:.2f}:  "
              f"false cutoffs {100*co:5.1f}%   endable early {100*ea:5.1f}%   [{verdict}]")
    print(f"  {'threshold':>10} {'false cutoffs':>15} {'endable early':>15}")
    for thr in (0.50, 0.70, 0.83, 0.90, 0.97, 0.99):
        fires = probs >= thr
        co = (fires & neg).sum() / max(1, neg.sum())
        ea = (fires & pos).sum() / max(1, pos.sum())
        print(f"  {thr:>10.2f} {100*co:>14.1f}% {100*ea:>14.1f}%")


def main() -> int:
    X, y, g = load_features()
    Xte, yte = holdout(X, y, g)
    print(f"holdout: {len(Xte)} bursts  "
          f"({int((yte==1).sum())} real turn ends / {int((yte==0).sum())} real interruptions)")
    print("Split by CALL, seed 0 — identical to train_real_turn_detector.py.")

    for label, path, kind in (
        ("SHIPPED  (trained on 2,754 all-positive rows)", SHIPPED, "forest"),
        ("RETRAINED forest (1,707 real / 1,243 real negatives)", RETRAINED, "forest"),
        ("RETRAINED linear", RETRAINED_LINEAR, "linear"),
    ):
        if not path.exists():
            print(f"\n=== {label} ===\n  MISSING: {path}")
            continue
        m = json.loads(path.read_text(encoding="utf-8"))
        if kind == "forest":
            probs = np.array([forest_probability(m, x) for x in Xte])
        else:
            probs = np.array([linear_probability(m, x) for x in Xte])
        report(label, probs, yte, m.get("threshold"))
    return 0


def fine_sweep():
    """The promotion decision needs finer resolution than the table above.

    `model_registry.py`'s rule is two-sided: the cut-off rate must not get worse
    AND the early-end rate must improve. The coarse table showed the retrained
    linear model straddling that line between 0.70 and 0.83, which is exactly
    where the answer lives, so the grid is refined there rather than eyeballed.
    """
    X, y, g = load_features()
    Xte, yte = holdout(X, y, g)
    neg, pos = (yte == 0), (yte == 1)

    ship = json.loads(SHIPPED.read_text(encoding="utf-8"))
    sp = np.array([forest_probability(ship, x) for x in Xte])
    s_fires = sp >= ship["threshold"]
    s_co = (s_fires & neg).sum() / max(1, neg.sum())
    s_ea = (s_fires & pos).sum() / max(1, pos.sum())
    print(f"\nSHIPPED baseline to beat: cutoffs {100*s_co:.1f}%  early {100*s_ea:.1f}%")
    print("Promotion rule: cutoffs must NOT get worse AND early-end must improve.\n")

    for label, path, kind in (("linear", RETRAINED_LINEAR, "linear"),
                              ("forest", RETRAINED, "forest")):
        m = json.loads(path.read_text(encoding="utf-8"))
        probs = (np.array([linear_probability(m, x) for x in Xte]) if kind == "linear"
                 else np.array([forest_probability(m, x) for x in Xte]))
        best = None
        for thr in np.arange(0.50, 0.95, 0.005):
            fires = probs >= thr
            co = (fires & neg).sum() / max(1, neg.sum())
            ea = (fires & pos).sum() / max(1, pos.sum())
            if co <= 0.02 and co <= s_co and ea >= s_ea:
                if best is None or ea > best[2]:
                    best = (thr, co, ea)
        if best:
            print(f"  {label:>7}: PROMOTABLE at threshold {best[0]:.3f} — "
                  f"cutoffs {100*best[1]:.1f}% (was {100*s_co:.1f}%), "
                  f"early {100*best[2]:.1f}% (was {100*s_ea:.1f}%)")
        else:
            print(f"  {label:>7}: no threshold satisfies both clauses of the rule")


if __name__ == "__main__":
    if "--fine" in sys.argv:
        raise SystemExit(fine_sweep())
    raise SystemExit(main())
