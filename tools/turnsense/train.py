#!/usr/bin/env python
"""Train TurnSense-te on real pauses and export it as plain JSON for the server.

Features come from `api.services.vaani.turnsense` -- the SAME module the live
analyzer imports -- so what is trained is exactly what runs.

Regularisation is chosen by 5-fold cross-validation GROUPED BY CALL on the
training rows (a 15% validation split of old calls held ~20 pauses, too few to
choose anything on). The fresh corpus is reported as TEST and never used to
choose anything; the replay on it (cv_replay.py) is the number that decides.

--teacher stacks a text-only scorer trained on grammar labels from gpt-oss-120b
(teacher_label.py). Its logit enters as ONE dense feature, "text_score"; the
behavioural model learns from real pauses how far to trust it.

Baselines printed beside it, so the model has to beat something real:
    rule      `completeness.sounds_unfinished` -- what production reads today
    punct     Soniox's own end punctuation alone

    python tools/turnsense/train.py [--teacher] [--fold k | --all] [--out path]
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

import numpy as np
from scipy import sparse
from sklearn.feature_extraction import DictVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import GroupKFold

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

REPO = Path(__file__).resolve().parents[2]
VAANI = REPO.parent / "dograh-vapi"
sys.path.insert(0, str(VAANI))
from api.services.vaani import turnsense as TS  # noqa: E402

DATA = REPO / ".tmp" / "turnsense" / "pauses.jsonl"
TEACHER = REPO / ".tmp" / "turnsense" / "teacher.jsonl"
OUT = VAANI / "api" / "services" / "vaani" / "models" / "turnsense_te.json"
CS = (0.03, 0.1, 0.3, 1.0, 3.0)


def load() -> list[dict]:
    return [json.loads(x) for x in DATA.read_text(encoding="utf-8").splitlines() if x]


def auc(y, p) -> float:
    return float(roc_auc_score(y, p)) if len(set(y)) > 1 else float("nan")


def grouped_cv_auc(X, y, groups, C, folds=5) -> float:
    y = np.asarray(y)
    pred = np.zeros(len(y))
    for tr, te in GroupKFold(n_splits=folds).split(X, y, groups):
        m = LogisticRegression(C=C, max_iter=4000, solver="liblinear")
        m.fit(X[tr], y[tr])
        pred[te] = m.predict_proba(X[te])[:, 1]
    return auc(y, pred)


# --------------------------------------------------------------------------
# the stacked text scorer
# --------------------------------------------------------------------------

def fit_text_model(min_conf: float = 60.0) -> dict | None:
    if not TEACHER.exists():
        return None
    rows = [json.loads(x) for x in TEACHER.read_text(encoding="utf-8").splitlines() if x]
    rows = [r for r in rows if r.get("conf", 0) >= min_conf]
    if len(rows) < 200:
        return None
    keys = [{k: 1.0 for k in TS.ngram_keys(r["agent"], r["text"], r["text"])} for r in rows]
    y = np.array([1 if r["complete"] else 0 for r in rows])
    groups = [r["call"] for r in rows]
    dv = DictVectorizer()
    X = dv.fit_transform(keys).tocsr()
    best = max(((grouped_cv_auc(X, y, groups, C), C) for C in CS))
    m = LogisticRegression(C=best[1], max_iter=4000, solver="liblinear").fit(X, y)
    inv = {i: k for k, i in dv.vocabulary_.items()}
    w = {inv[i]: round(float(c), 5) for i, c in enumerate(m.coef_[0]) if abs(c) > 1e-4}
    print(f"text scorer: {len(rows)} teacher rows (complete {y.mean():.0%}), "
          f"grouped-CV AUC {best[0]:.3f} at C={best[1]}, {len(w)} weights")
    return {"w_sparse": w, "intercept": float(m.intercept_[0]), "n": len(rows),
            "cv_auc": round(best[0], 4)}


def text_score(tm: dict | None, r: dict) -> float:
    if not tm:
        return 0.0
    z = tm["intercept"] + sum(tm["w_sparse"].get(k, 0.0)
                              for k in set(TS.ngram_keys(r["agent"], r["turn"], r["last"])))
    return max(-8.0, min(8.0, z))


# --------------------------------------------------------------------------
# the behavioural model
# --------------------------------------------------------------------------

def featurize(rows, tm=None, dv=None, mean=None, scale=None, names=None):
    dense = []
    for r in rows:
        d = TS.dense_features(r["agent"], r["turn"], r["last"], r["seg_secs"], r["n_segs"],
                              r.get("caller_hold_rate", TS.HOLD_PRIOR),
                              r.get("caller_pauses", 0))
        if tm:
            d["text_score"] = text_score(tm, r)
        dense.append(d)
    if names is None:
        names = sorted(dense[0].keys())
    D = np.array([[d.get(n, 0.0) for n in names] for d in dense], dtype=np.float64)
    if mean is None:
        mean = D.mean(axis=0)
        scale = D.std(axis=0)
        scale[scale < 1e-9] = 1.0
        mean[names.index("bias")] = 0.0
        scale[names.index("bias")] = 1.0
    Dz = (D - mean) / scale
    keys = [{k: 1.0 for k in TS.ngram_keys(r["agent"], r["turn"], r["last"])} for r in rows]
    if dv is None:
        dv = DictVectorizer()
        S = dv.fit_transform(keys)
    else:
        S = dv.transform(keys)
    X = sparse.hstack([sparse.csr_matrix(Dz), S]).tocsr()
    return X, dv, mean, scale, names


def report(name, y, p):
    y = np.asarray(y)
    p = np.asarray(p)
    out = [f"{name:10} AUC {auc(y, p):.3f}"]
    for t in (0.5, 0.7, 0.8, 0.9):
        m = p >= t
        if m.sum():
            out.append(f"p>={t}: fires {m.mean():5.1%}, wrong {1 - y[m].mean():5.1%}")
    print("   ".join(out))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-export", action="store_true")
    ap.add_argument("--fold", type=int, help="k-fold over the fresh calls: test = fold k")
    ap.add_argument("--nfolds", type=int, default=5)
    ap.add_argument("--all", action="store_true",
                    help="final model: train on old calls + ALL fresh calls")
    ap.add_argument("--teacher", action="store_true", help="stack the grammar scorer")
    ap.add_argument("--no-sparse", action="store_true",
                    help="dense features (+ text_score) only")
    ap.add_argument("--out", help="artifact path (default: the shipped one)")
    a = ap.parse_args()

    rows = load()
    tr = [r for r in rows if r["split"] in ("train", "val")]
    te = [r for r in rows if r["split"] == "test"]
    if a.fold is not None or a.all:
        calls = sorted({r["call"] for r in te})
        random.Random(7).shuffle(calls)
        fold_of = {c: i % a.nfolds for i, c in enumerate(calls)}
        if a.all:
            tr = tr + te
        else:
            tr = tr + [r for r in te if fold_of[r["call"]] != a.fold]
            te = [r for r in te if fold_of[r["call"]] == a.fold]
        (OUT.parent / "cand").mkdir(parents=True, exist_ok=True)
        if a.fold is not None:
            (OUT.parent / "cand" / f"fold{a.fold}_calls.txt").write_text(
                "\n".join(sorted({r["call"] for r in te})), encoding="utf-8")
    print(f"train {len(tr)}  test {len(te)}   SHIFT rate train "
          f"{np.mean([r['label'] for r in tr]):.2f} test "
          f"{np.mean([r['label'] for r in te]):.2f}")

    tm = fit_text_model() if a.teacher else None
    Xtr, dv, mean, scale, names = featurize(tr, tm)
    Xte = featurize(te, tm, dv, mean, scale, names)[0]
    nd = len(names)
    if a.no_sparse:
        Xtr, Xte = Xtr[:, :nd], Xte[:, :nd]
    ytr = np.array([r["label"] for r in tr])
    yte = np.array([r["label"] for r in te])
    groups = [r["call"] for r in tr]

    scored = [(grouped_cv_auc(Xtr, ytr, groups, C), C) for C in CS]
    for s, C in scored:
        print(f"  C={C:<5} grouped-CV AUC {s:.3f}")
    cv, C = max(scored)
    model = LogisticRegression(C=C, max_iter=4000, solver="liblinear").fit(Xtr, ytr)
    print(f"chosen C={C}")

    if len(te):
        rule = [0.0 if TS.sounds_unfinished(r["last"]) else 1.0 for r in te]
        pun = [{"q": .9, "stop": .8, "none": .5, "comma": .2, "trail": .1}[
            TS._end_punct(r["last"])] for r in te]
        print(f"--- TEST ({len(te)} pauses) ---")
        report("rule", yte, rule)
        report("punct", yte, pun)
        if tm:
            report("text-only", yte, [1 / (1 + np.exp(-text_score(tm, r))) for r in te])
        report("turnsense", yte, model.predict_proba(Xte)[:, 1])

    if a.no_export:
        return 0
    coef = model.coef_[0]
    inv = {i: k for k, i in dv.vocabulary_.items()}
    w_sparse = {} if a.no_sparse else {
        inv[i]: round(float(coef[nd + i]), 5)
        for i in range(len(inv)) if abs(coef[nd + i]) > 1e-4}
    blob = {
        "version": "turnsense-te-v1" + ("+teacher" if tm else ""),
        "dense_names": names,
        "dense_mean": [round(float(v), 6) for v in mean],
        "dense_scale": [round(float(v), 6) for v in scale],
        "w_dense": [round(float(v), 6) for v in coef[:nd]],
        "w_sparse": w_sparse,
        "intercept": float(model.intercept_[0]),
        "meta": {"C": C, "train_rows": len(tr), "grouped_cv_auc": round(cv, 4),
                 "test_auc": round(auc(yte, model.predict_proba(Xte)[:, 1]), 4)
                 if len(te) else None,
                 "labels": "HOLD=resumed<=1.5s, SHIFT=silent>=2.5s, reprompt->SHIFT",
                 "stt": "soniox stt-rt-v5 India, finalize on Silero stop"},
    }
    if tm:
        blob["text_model"] = {"w_sparse": tm["w_sparse"], "intercept": tm["intercept"],
                              "n": tm["n"], "cv_auc": tm["cv_auc"]}
    out = Path(a.out) if a.out else OUT
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(blob, ensure_ascii=False), encoding="utf-8")
    # parity: the exported JSON must reproduce sklearn's probabilities
    tmodel = TS.TurnSenseModel.load(out)
    chk = te[:300] if len(te) else tr[:300]
    Xc = Xte[:300] if len(te) else Xtr[:300]
    mx = max(abs(tmodel.probability(r["agent"], r["turn"], r["last"], r["seg_secs"],
                                    r["n_segs"], r.get("caller_hold_rate", TS.HOLD_PRIOR),
                                    r.get("caller_pauses", 0)) - p)
             for r, p in zip(chk, model.predict_proba(Xc)[:, 1]))
    print(f"exported {out.name}: {len(w_sparse)} sparse weights, "
          f"{out.stat().st_size / 1024:.0f} KB, max |p_json - p_sklearn| = {mx:.1e}")
    if mx > 1e-3:
        print("PARITY FAILED -- the exported model does not match what was trained")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
