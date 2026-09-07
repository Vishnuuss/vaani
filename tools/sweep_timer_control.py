"""The trade curve (tools/sweep_timer_control.py)

Run: python tools/sweep_timer_control.py [N_CALLS]

The trade curve: cut-offs vs wait, for a DUMB TIMER and for each model.

The control showed a model-free 2.2s timer beating smart-turn-v3 at the same
wait. If the dumb timer's curve sits BELOW every model at equal wait, then the
prosody models are not buying speed -- they are paying cut-offs for nothing,
and the honest fix is a constant plus a semantic signal, not a better forest.
"""
import statistics, sys, json
from pathlib import Path
REPO = Path(r"c:\Users\vishnu\Downloads\bswealthfinance")
sys.path.insert(0, str(REPO)); sys.path.insert(0, str(REPO.parent / "dograh-vapi"))
import importlib.util
spec = importlib.util.spec_from_file_location("replay_turns", REPO / "tools" / "replay_turns.py")
RT = importlib.util.module_from_spec(spec); spec.loader.exec_module(RT)
import api.services.vaani.telugu_turn as tt

def _run_for(wav):
    try: wf, rid = wav.stem.split("_run")
    except ValueError: return None
    log = REPO / ".tmp" / "harvest" / "runs" / f"{wf[2:]}_{rid}.json"
    return json.loads(log.read_text(encoding="utf-8")) if log.exists() else None

LIMIT = int(sys.argv[1]) if len(sys.argv) > 1 else 60
calls = sorted((REPO / ".tmp" / "audio" / "caller").glob("*.wav"))[:LIMIT]

def dumb(w):
    # threshold 1.1 = unreachable, so NO model verdict ever fires: pure timer.
    return dict(params=tt.TeluguTurnParams(
        threshold=1.1, min_endpoint_secs=w, max_endpoint_secs=w + 0.1,
        unsure_floor_secs=w, fragment_floor_secs=w))

CASES = []
for w in (0.4, 0.6, 0.8, 1.0, 1.3, 1.6, 2.0):
    CASES.append((f"DUMB TIMER {w:.1f}s", dumb(w)))
CASES += [
    ("MODEL shipped (live)",  dict()),
    ("MODEL real-linear",     dict(model="real-linear")),
    ("MODEL real-gbm",        dict(model="real-gbm")),
    ("MODEL smart-turn-v3",   dict(detector="smart-turn-v3")),
]

print(f"\n{len(calls)} recordings, 235 bursts expected\n")
print(f"{'case':24} {'CUT OFF':>15} {'wait p50':>9} {'wait p90':>9}")
print("-" * 62)
rows = []
for name, kw in CASES:
    bursts = cut = 0; waits = []
    for wav in calls:
        run = _run_for(wav)
        if run is None: continue
        try: r = RT.replay(wav, run, **kw)
        except Exception: continue
        if r.get("error"): continue
        bursts += r.get("bursts", 0); cut += r.get("cutoffs", 0)
        waits.extend(r.get("waits", []))
    pct = 100.0 * cut / bursts if bursts else 0.0
    p50 = statistics.median(waits) if waits else 0.0
    p90 = sorted(waits)[int(len(waits)*0.9)] if len(waits) > 9 else p50
    rows.append((name, pct, p50))
    print(f"{name:24} {cut:>5} ({pct:>5.1f}%) {p50:>9.2f} {p90:>9.2f}")
    sys.stdout.flush()

print("\n--- Is any model better than a timer at the same wait? ---")
timers = sorted([(p50, pct) for n, pct, p50 in rows if n.startswith("DUMB")])
def interp(w):
    if w <= timers[0][0]: return timers[0][1]
    if w >= timers[-1][0]: return timers[-1][1]
    for (w0,c0),(w1,c1) in zip(timers, timers[1:]):
        if w0 <= w <= w1:
            return c0 + (c1-c0)*(w-w0)/(w1-w0 or 1)
    return timers[-1][1]
for name, pct, p50 in rows:
    if name.startswith("DUMB"): continue
    base = interp(p50)
    verdict = "BETTER than a timer" if pct < base - 0.5 else "WORSE than a timer"
    print(f"{name:24} {pct:5.1f}% at {p50:.2f}s  vs timer {base:5.1f}%  -> {verdict}")
