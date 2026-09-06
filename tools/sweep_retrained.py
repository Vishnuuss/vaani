#!/usr/bin/env python
"""Find the endpoint window the RETRAINED model deserves, by measuring it.

Why this is not a guess
-----------------------
The endpoint floors in production -- `min_endpoint_secs` 0.70,
`max_endpoint_secs` 1.40, `fragment_floor_secs` 1.00, `blind_min_silence_ms`
250, `blind_short_silence_ms` 450 -- were all sized for a model that, measured
on real negatives for the first time on 7 Sep, barely discriminates: its
cut-off rate tracks its early-end rate at every threshold (43.4%/42.7% at 0.83,
27.6%/29.4% at 0.90). Floors are what you add when the model cannot be trusted.

Inheriting those floors for a model that CAN discriminate is how the retrained
detector ended up looking like a wash end-to-end: 19.1% cut off vs 23.0%
shipped, but with the wait p50 doubled from 0.48s to 0.98s. That is the floors
talking, not the model.

`_wait_secs` interpolates the wait between `min_endpoint_secs` and
`max_endpoint_secs` on `frac = p / bar`. With a model whose p actually means
something, the two ends should be FURTHER apart, not closer: a caller the model
is sure about should be answered fast, and a caller it is sure is mid-sentence
should be given real time -- more than the 1.40s ceiling currently allows.

So the window is swept rather than picked. Every number this prints is measured
on the same 60 recorded calls, and none of them is hardcoded anywhere.

    python tools/sweep_retrained.py
    python tools/sweep_retrained.py --limit 100
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO.parent / "dograh-vapi"))


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, REPO / "tools" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    argv, sys.argv = sys.argv, [name]
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.argv = argv
    return mod


def jobs(rt, limit: int):
    out = []
    for wav in sorted(rt.CALLER_DIR.glob("*.wav"))[:limit]:
        try:
            wf, rid = wav.stem.split("_run")
        except ValueError:
            continue
        log = rt.RUNS_DIR / f"{wf[2:]}_{rid}.json"
        if log.exists():
            out.append((wav, json.loads(log.read_text(encoding="utf-8"))))
    return out


def score(rt, tt, work, model: str, **overrides) -> tuple[float, float, float, int]:
    """Cut-off rate and wait percentiles for one configuration."""
    import statistics
    bursts = cut = 0
    waits: list[float] = []
    for wav, run in work:
        p = tt.TeluguTurnParams(**overrides)
        r = rt.replay(wav, run, params=p, model=model)
        bursts += r["bursts"]
        # `cutoffs` is the analyzer's OWN counter -- the same field the CLI
        # totals. `cut_n` counts something else and reads 0 on every config,
        # which is how a first version of this sweep printed a flat 0.0%.
        cut += r["cutoffs"]
        waits.extend(r.get("waits") or [])
    p50 = statistics.median(waits) if waits else 0.0
    p90 = (statistics.quantiles(waits, n=10)[8] if len(waits) >= 10
           else (max(waits) if waits else 0.0))
    return (100.0 * cut / max(1, bursts), p50, p90, bursts)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=60)
    a = ap.parse_args()

    rt = _load("replay_turns")
    import api.services.vaani.telugu_turn as tt

    work = jobs(rt, a.limit)
    print(f"{len(work)} recorded calls\n")

    print(f"{'config':<46} {'cut off':>9} {'wait p50':>9} {'wait p90':>9}")
    print("-" * 76)

    base = score(rt, tt, work, "shipped")
    print(f"{'SHIPPED, production floors (today)':<46} "
          f"{base[0]:>8.1f}% {base[1]:>8.2f}s {base[2]:>8.2f}s")

    # The retrained model wearing the OLD floors -- the comparison that made the
    # retrain look like a wash.
    r = score(rt, tt, work, "real-linear")
    print(f"{'retrained linear, SAME old floors':<46} "
          f"{r[0]:>8.1f}% {r[1]:>8.2f}s {r[2]:>8.2f}s")

    print()
    # Widen the window. `max_endpoint_secs` is the wait given to a caller the
    # model says is clearly still going; 1.40s is the current ceiling and the
    # p90 is already pressed against it, which means the ceiling is binding.
    for mx in (1.40, 2.00, 2.50, 3.00):
        for mn in (0.70, 0.40, 0.25):
            r = score(rt, tt, work, "real-linear",
                      min_endpoint_secs=mn, max_endpoint_secs=mx)
            tag = f"retrained linear, min {mn:.2f} / max {mx:.2f}"
            flag = ""
            if r[0] < base[0] and r[1] <= base[1]:
                flag = "  <-- fewer cut-offs AND no slower"
            print(f"{tag:<46} {r[0]:>8.1f}% {r[1]:>8.2f}s {r[2]:>8.2f}s{flag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
