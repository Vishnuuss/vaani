#!/usr/bin/env python
"""Find the wait mapping the audio-native model deserves.

The result this exists to explain
----------------------------------
End to end on 60 calls, 235 bursts:

    prosody forest (live)   23.0% cut off   wait p50 0.48s
    audio-native            17.0% cut off   wait p50 1.14s

Six points better, and still not good enough, because a model-free stopwatch at
that same wait scores about 15.9% (docs 30). The classifier is genuinely far
better -- AUC 0.670 against prosody's 0.550 -- and it is being wasted.

Why: `TeluguTurnAnalyzer._wait_secs` interpolates the wait on `frac = p / bar`,
and every constant in it was tuned around the FOREST's calibration, where the
threshold is 0.97. The audio model's threshold is 0.936 and its probabilities
are distributed differently, so a correct verdict is being mapped onto the wrong
amount of patience. `AudioNativeTurnAnalyzer` inherits that mapping deliberately
-- it preserves every regression the timers already pass -- which makes fixing
the mapping a separate, measurable job rather than a rewrite.

So this sweeps the endpoint window for the audio model and scores each setting
against the stopwatch curve at the SAME wait. A setting only counts as a win if
it beats a timer that costs the caller the same silence.

    python tools/sweep_audio_native.py [N_CALLS]
"""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DOGRAH = REPO.parent / "dograh-vapi"
sys.path.insert(0, str(DOGRAH / "pipecat" / "src"))
sys.path.insert(0, str(DOGRAH))

import importlib.util  # noqa: E402

spec = importlib.util.spec_from_file_location("replay_turns", REPO / "tools" / "replay_turns.py")
RT = importlib.util.module_from_spec(spec)
spec.loader.exec_module(RT)
import api.services.vaani.telugu_turn as tt  # noqa: E402

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

# The stopwatch curve, measured in docs 30 on this same set: (wait p50, cut-off%).
# A model is only worth its inference cost if it lands BELOW this line.
TIMER_CURVE = [(0.60, 27.7), (0.80, 19.1), (1.00, 16.6),
               (1.20, 15.7), (1.52, 13.6), (1.84, 9.4), (2.24, 5.1)]


def timer_at(wait: float) -> float:
    if wait <= TIMER_CURVE[0][0]:
        return TIMER_CURVE[0][1]
    if wait >= TIMER_CURVE[-1][0]:
        return TIMER_CURVE[-1][1]
    for (w0, c0), (w1, c1) in zip(TIMER_CURVE, TIMER_CURVE[1:]):
        if w0 <= wait <= w1:
            return c0 + (c1 - c0) * (wait - w0) / (w1 - w0)
    return TIMER_CURVE[-1][1]


def _run_for(wav: Path):
    try:
        wf, rid = wav.stem.split("_run")
    except ValueError:
        return None
    log = REPO / ".tmp" / "harvest" / "runs" / f"{wf[2:]}_{rid}.json"
    return json.loads(log.read_text(encoding="utf-8")) if log.exists() else None


GRID = {
    "inherited (as measured)": {},
    "min 0.25 / max 1.20":     dict(min_endpoint_secs=0.25, max_endpoint_secs=1.20),
    "min 0.30 / max 1.00":     dict(min_endpoint_secs=0.30, max_endpoint_secs=1.00),
    "min 0.40 / max 1.40":     dict(min_endpoint_secs=0.40, max_endpoint_secs=1.40),
    "min 0.20 / max 0.90":     dict(min_endpoint_secs=0.20, max_endpoint_secs=0.90),
    # The unsure band decides who gets the floor. Widening it spends patience
    # only on the turns the model is not sure about, which is where the
    # cut-offs actually are.
    "band 0.90, min 0.30":     dict(unsure_band=0.90, min_endpoint_secs=0.30,
                                    max_endpoint_secs=1.40),
}


def main() -> int:
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    calls = sorted((REPO / ".tmp" / "audio" / "caller").glob("*.wav"))[:limit]
    print(f"\n{len(calls)} recordings, audio-native detector\n")
    print(f"{'setting':26} {'CUT OFF':>15} {'wait p50':>9} {'timer':>8} {'verdict':>12}")
    print("-" * 76)

    for name, over in GRID.items():
        params = tt.TeluguTurnParams(**over) if over else None
        bursts = cut = 0
        waits: list[float] = []
        for wav in calls:
            run = _run_for(wav)
            if run is None:
                continue
            try:
                r = RT.replay(wav, run, params=params, detector="audio-native")
            except Exception:
                continue
            if r.get("error"):
                continue
            bursts += r.get("bursts", 0)
            cut += r.get("cutoffs", 0)
            waits.extend(r.get("waits", []))
        if not bursts:
            print(f"{name:26} no data")
            continue
        pct = 100.0 * cut / bursts
        p50 = statistics.median(waits) if waits else 0.0
        base = timer_at(p50)
        verdict = "BEATS timer" if pct < base - 0.5 else "worse"
        print(f"{name:26} {cut:>4} ({pct:>5.1f}%) {p50:>9.2f} {base:>7.1f}% {verdict:>12}")
        sys.stdout.flush()

    print("\nA setting is only worth the model's inference cost if it lands")
    print("BELOW the stopwatch at the same wait. Anything else is patience the")
    print("caller could have had for free.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
