#!/usr/bin/env python
"""5-fold cross-validated replay: every fresh call is scored by a model that never saw it.

    fold k:  train on old calls + fresh calls outside fold k   (train.py --fold k)
             replay fold k's calls with that model               (replay_eval.replay_one)
    sum the five folds -> one cut-off rate and one wait distribution over all
    fresh calls, each scored out-of-sample.

Then the same grid of analyzer settings is swept over those out-of-sample runs,
and every row is compared with the two numbers it has to beat, measured on the
same calls with the same instrument:

    live (0.7 s timer racing the Telugu assist)   9.6% cut off   wait p50 0.88 s
    0.7 s timer alone                             7.4%           0.88 s

    python tools/turnsense/cv_replay.py            # trains folds if missing
"""
from __future__ import annotations

import argparse
import itertools
import json
import statistics
import subprocess
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tools" / "turnsense"))
import replay_eval as RE  # noqa: E402

CAND = REPO.parent / "dograh-vapi" / "api" / "services" / "vaani" / "models" / "cand"
LIVE = (9.6, 0.88)
TIMER = (7.4, 0.88)


def train_folds(n: int, force: bool, tag: str = "", extra: list[str] | None = None):
    for k in range(n):
        out = CAND / f"fold{k}{tag}.json"
        if out.exists() and not force:
            continue
        r = subprocess.run([sys.executable, str(REPO / "tools" / "turnsense" / "train.py"),
                            "--fold", str(k), "--nfolds", str(n), "--out", str(out),
                            *(extra or [])],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace")
        if r.returncode != 0:
            raise SystemExit(f"fold {k} training FAILED (rc={r.returncode}): "
                             + r.stdout[-800:] + r.stderr[-800:])
        tail = [x for x in r.stdout.splitlines() if "turnsense" in x and "AUC" in x]
        print(f"  fold {k}: {tail[-1] if tail else r.stdout[-300:] + r.stderr[-300:]}",
              flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--folds", type=int, default=5)
    ap.add_argument("--retrain", action="store_true")
    ap.add_argument("--workers", type=int, default=11)
    ap.add_argument("--grid", default="default")
    ap.add_argument("--save", default=str(REPO / ".tmp" / "turnsense" / "cv_results.json"))
    ap.add_argument("--tag", default="", help="fold artifact suffix, e.g. _t")
    ap.add_argument("--train-args", default="", help="extra train.py args, e.g. --teacher")
    a = ap.parse_args()

    print("training fold models ...", flush=True)
    train_folds(a.folds, a.retrain, a.tag, a.train_args.split())

    jobs = {j[0]: j for j in RE.calls(RE.FRESH, need_rt=True)}
    folds = {}
    placed = set()
    for k in range(a.folds):
        stems = (CAND / f"fold{k}_calls.txt").read_text(encoding="utf-8").split()
        folds[k] = [jobs[s] for s in stems if s in jobs]
        placed |= set(stems)
    # Calls with no labelled pause never entered training at all, so ANY fold's
    # model is out-of-sample for them. Dropping them would score the new policy
    # on a different set of calls from the baselines.
    for i, s in enumerate(sorted(set(jobs) - placed)):
        folds[i % a.folds].append(jobs[s])
    print("calls per fold:", {k: len(v) for k, v in folds.items()}, flush=True)

    if a.grid == "default":
        grid = [dict(fast=f, slow=s, max=m, mid=0.5, notext=0.7)
                for f, s, m in itertools.product((0.6, 0.7, 0.8, 0.9),
                                                 (0.2, 0.35, 0.5),
                                                 (1.0, 1.2, 1.5))
                if s < f]
    else:
        grid = [dict(kv.split("=") for kv in g.split(",")) for g in a.grid.split(";")]

    specs = []
    for g in grid:
        base = "turnsense:" + ",".join(f"{k}={v}" for k, v in g.items())
        specs.append(base)
    tasks, index = [], []
    # Baselines on EXACTLY these calls, same run, same instrument.
    for bl in ("live", "timer:0.70", "timer:0.50"):
        for js in folds.values():
            for s, w, l in js:
                tasks.append((s, str(w), str(l), bl, "rt"))
                index.append(bl)
    for spec in specs:
        for k, js in folds.items():
            pol = f"{spec},model={CAND / f'fold{k}{a.tag}.json'}"
            for s, w, l in js:
                tasks.append((s, str(w), str(l), pol, "rt"))
                index.append(spec)
    print(f"{len(specs)} settings x {sum(len(v) for v in folds.values())} calls "
          f"= {len(tasks)} replays", flush=True)
    with ProcessPoolExecutor(max_workers=a.workers) as ex:
        rows = list(ex.map(RE.replay_one, tasks, chunksize=4))

    agg: dict[str, dict] = {}
    for spec, r in zip(index, rows):
        d = agg.setdefault(spec, {"bursts": 0, "cut": 0, "good": []})
        d["bursts"] += r["bursts"]
        d["cut"] += r["cutoffs"]
        d["good"] += r["good_waits"]
    out = []
    for spec, d in agg.items():
        q = sorted(d["good"])
        cut = 100.0 * d["cut"] / max(1, d["bursts"])
        p50 = statistics.median(q) if q else float("nan")
        p90 = q[int(len(q) * 0.9)] if len(q) > 9 else float("nan")
        out.append({"spec": spec, "bursts": d["bursts"], "cut": d["cut"],
                    "cut_pct": cut, "wait_p50": p50, "wait_p90": p90})
    base = {x["spec"]: x for x in out if not x["spec"].startswith("turnsense")}
    out = [x for x in out if x["spec"].startswith("turnsense")]
    out.sort(key=lambda x: (x["cut_pct"], x["wait_p50"]))
    LIVE = (base["live"]["cut_pct"], base["live"]["wait_p50"])
    TIMER = (base["timer:0.70"]["cut_pct"], base["timer:0.70"]["wait_p50"])
    print(f"\n{'setting (5-fold, out-of-sample)':52} {'bursts':>6} {'cut%':>6} "
          f"{'wait50':>7} {'wait90':>7}  verdict")
    for name, x in base.items():
        print(f"{'BASELINE ' + name:52} {x['bursts']:>6} {x['cut_pct']:>5.1f}% "
              f"{x['wait_p50']:>7.2f} {x['wait_p90']:>7.2f}")
    for x in out:
        v = []
        if x["cut_pct"] <= LIVE[0] and x["wait_p50"] < LIVE[1]:
            v.append("beats LIVE on both")
        if x["cut_pct"] <= TIMER[0] and x["wait_p50"] < TIMER[1]:
            v.append("beats TIMER on both")
        print(f"{x['spec'][10:]:52} {x['bursts']:>6} {x['cut_pct']:>5.1f}% "
              f"{x['wait_p50']:>7.2f} {x['wait_p90']:>7.2f}  {', '.join(v)}")
    Path(a.save).write_text(json.dumps({"baselines": base, "grid": out}, indent=1),
                            encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
