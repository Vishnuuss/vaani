#!/usr/bin/env python
"""The trade curve for the knobs that actually reach a CONFIDENT decision.

`sweep_endpoint.py` sweeps `min_endpoint_secs`, and on 23 September that turned
out to be the wrong axis. `TeluguTurnAnalyzer.append_audio` has two exits:

    fast   telugu_turn.py:711   p >= bar and not blind   -> COMPLETE
    timed  telugu_turn.py:715   silence >= _wait_secs()  -> COMPLETE

`_wait_secs()` is called from :715 and nowhere else, so every value it
interpolates -- `min_endpoint_secs`, `max_endpoint_secs`, `unsure_floor_secs`,
`unsure_band` -- is unreachable from a confident turn, which exits at :711.
That was verified the expensive way: `endpoint_min_secs` was moved 0.3 -> 0.5 on
the live agent, published, and measured to change nothing, and the day was spent
concluding first that the setting was ignored and then that the analyzer was not
running. Neither was true.

Two knobs do reach the fast exit:

    need  = blind_min_silence_ms                                     (250)
    if speech_secs < fragment_secs: need = max(need, blind_short_silence_ms)
    blind = not text_is_fresh and silence_ms < need

  * `blind_min_silence_ms` is the floor on every confident release. It matches
    the 0.242-0.33s cluster measured live, exactly.
  * `turn_fragment_secs` decides which turns count as SHORT, and a short turn
    gets the 0.995 bar, the longer blind floor and the fragment wait floor. It
    is the only config-reachable way to make a confident decision more careful,
    so it is the anti-cutoff axis.

So this sweeps those two against each other, on real labelled Telugu audio, and
prints both failures side by side. A latency win bought with cut-offs is not a
win -- `latency_budget.yaml` says so in writing, and three separate live
attempts this project has made proved it the hard way.

    python tools/sweep_real_levers.py --limit 120
"""
from __future__ import annotations

import argparse
import importlib.util
import statistics
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO.parent / "dograh-vapi"))
_spec = importlib.util.spec_from_file_location(
    "replay_turns", REPO / "tools" / "replay_turns.py")
RT = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(RT)

import api.services.vaani.telugu_turn as tt  # noqa: E402

# The live values, so every row is read as a delta from what callers hear today.
LIVE_BLIND = 250.0
LIVE_FRAGMENT = 0.65

GRID: dict[str, dict] = {
    "LIVE (blind 250 / frag 0.65)": {},
    # Faster: lower the only floor a confident turn ever sees.
    "blind 180": {"blind_min_silence_ms": 180.0},
    "blind 150": {"blind_min_silence_ms": 150.0},
    # Safer: send more turns to the 0.995 bar and the long floors.
    "frag 1.00": {"fragment_secs": 1.00},
    "frag 1.20": {"fragment_secs": 1.20},
    # The combination that is the whole point -- fast when sure, careful on the
    # short bursts where this detector has always cut people off.
    "blind 180 + frag 1.00": {"blind_min_silence_ms": 180.0, "fragment_secs": 1.00},
    "blind 180 + frag 1.20": {"blind_min_silence_ms": 180.0, "fragment_secs": 1.20},
    "blind 150 + frag 1.20": {"blind_min_silence_ms": 150.0, "fragment_secs": 1.20},
}


def _run_for(wav: Path):
    """The cached run log beside a caller recording. Same rule as replay_turns."""
    import json
    try:
        wf, rid = wav.stem.split("_run")           # wf<W>_run<R>
    except ValueError:
        return None
    log = REPO / ".tmp" / "harvest" / "runs" / f"{wf[2:]}_{rid}.json"
    if not log.exists():
        return None
    return json.loads(log.read_text(encoding="utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=120)
    # Which corpus. `.tmp/audio/caller` is 651 recordings dated 2026-08-28 --
    # the day the Telugu detector went on the call path -- so it predates the
    # thing it is used to judge, and every cut-off number ever quoted from it
    # describes audio this detector never handled. `.tmp/audio_new/caller` is
    # the post-retrain harvest and is what a decision should be made on.
    ap.add_argument("--dir", default=".tmp/audio_new/caller")
    a = ap.parse_args()

    calls = sorted((REPO / a.dir).glob("*.wav"))[: a.limit]
    usable = [(w, r) for w in calls if (r := _run_for(w)) is not None]
    print(f"\n{len(usable)} recordings with run logs (of {len(calls)} wavs)\n")
    print(f"{'setting':30} {'bursts':>7} {'CUT OFF':>15} {'wait p50':>9} "
          f"{'wait p90':>9} {'slow':>6}")
    print("-" * 82)

    baseline = None
    for name, overrides in GRID.items():
        params = tt.TeluguTurnParams(**overrides)
        bursts = cut = slow = 0
        waits: list[float] = []
        for wav, run in usable:
            try:
                r = RT.replay(wav, run, params=params)
            except Exception:
                continue
            if r.get("error"):
                continue
            bursts += r.get("bursts", 0)
            cut += r.get("cutoffs", 0)
            slow += r.get("slow", 0)
            waits.extend(r.get("waits", []))
        if not bursts:
            print(f"{name:30} {'no bursts replayed':>40}")
            continue
        pct = 100.0 * cut / bursts
        p50 = statistics.median(waits) if waits else 0.0
        p90 = (sorted(waits)[int(len(waits) * 0.9)] if len(waits) > 9 else p50)
        if baseline is None:
            baseline = (pct, p50)
        d_cut = pct - baseline[0]
        d_wait = p50 - baseline[1]
        flag = ""
        if name != "LIVE (blind 250 / frag 0.65)":
            # Only a row that does not trade one failure for the other is
            # worth a phone call.
            flag = "  <-- BETTER ON BOTH" if (d_cut <= 0.05 and d_wait <= -0.005) else ""
        print(f"{name:30} {bursts:>7} {cut:>5} ({pct:>5.1f}%) {p50:>9.2f} "
              f"{p90:>9.2f} {slow:>6}{flag}")

    print("\nA row is only better if CUT OFF does not rise AND the wait falls.")
    print("Anything else has moved the problem, not fixed it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
