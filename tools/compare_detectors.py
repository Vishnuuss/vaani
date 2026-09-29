#!/usr/bin/env python
"""Dograh's turn detector against ours, on the calls each one actually ran.

The question
------------
"turn detection problem has appeared on the day we built our own turn detector."
Our Telugu detector went live 2026-08-28 (0ac025d). Before that date every call
used Dograh's. So the two eras are two populations, and the recordings say what
each detector did.

Why the earlier answers were worthless, recorded so they are not repeated
-------------------------------------------------------------------------
1. Keyword-matching transcripts for complaints returned 27 hits, of which 25
   were the identical seeded string "ఆగండి మాట్లాడనివ్వట్లేదు" across four
   workflows and 2 were false matches on the ordinary word "మధ్యలో". That
   measured the test harness, not callers. Retracted.
2. Every `replay_turns.py` cut-off number -- including 23% -> 14.5% -- was
   produced by replaying PRE-28-AUGUST audio through candidate detectors. A
   fair comparison between detectors on identical audio, and no evidence at all
   about live behaviour, because the corpus contains zero calls from the era
   under dispute.
3. Counting raw overlap between the two legs gave 76% and is echo: on a
   speakerphone the caller's leg carries our own voice, so "he was talking
   while we started" is often us hearing ourselves.

The metric here, chosen to be immune to (3)
--------------------------------------------
A cut-off is read from the CALLER's burst boundaries, never from overlap:

    he speaks, he pauses, WE start talking inside that pause, and he then
    carries on speaking.

If he carries on, the pause was not the end of his turn, and we took it for
one. The bot leg is used only for "did we start talking, and when" -- which is
our own track and cannot be echo. Nothing here depends on the caller's leg
being clean while we speak.

Also reported: the client's own hypothesis, that our detector "waits 500ms and
interrupts whether the user stopped or not". If true, the silence between his
burst ending and our speech starting is a near-constant. A detector that
decides per turn produces a spread. The p10/p50/p90 spread answers it.

Honest limit
------------
The two eras are different calls, different callers and partly different
scripts, so this is observational, not controlled. It is the only direct
evidence of live behaviour that exists, and it is reported as such.

    python tools/compare_detectors.py
"""
from __future__ import annotations

import json
import sys
import wave
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))
import true_latency as TL  # noqa: E402

ERAS = {
    "DOGRAH  (to 27 Aug)": (REPO / ".tmp" / "audio" / "caller",
                            REPO / ".tmp" / "audio" / "bot"),
    "OURS    (28 Aug on)": (REPO / ".tmp" / "audio_new" / "caller",
                            REPO / ".tmp" / "audio_new" / "bot"),
}

# He paused this long or less and then carried on -> the pause was not a turn
# end. Matches telugu_turn.RESUME_WINDOW_S so there is one definition of
# "we cut him off" in the project, not two.
RESUME_S = 1.0
# Ignore a scrap of speech after we start; it has to be real continuation.
MIN_RESUME_S = 0.30

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")


def _regions(path: Path):
    with wave.open(str(path)) as w:
        sr = w.getframerate()
        x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    return TL.regions(x.astype(np.float32) / 32768.0, sr)


def score(caller_dir: Path, bot_dir: Path) -> dict:
    stems = sorted({p.stem for p in caller_dir.glob("*.wav")} &
                   {p.stem for p in bot_dir.glob("*.wav")})
    calls = 0
    opportunities = 0        # caller pauses where we could have cut in
    cutoffs = 0
    waits: list[float] = []  # silence between his burst ending and us starting

    for stem in stems:
        try:
            caller = _regions(caller_dir / f"{stem}.wav")
            bot = _regions(bot_dir / f"{stem}.wav")
        except Exception:
            continue
        if not caller or not bot:
            continue
        calls += 1
        bot_starts = [s for s, _ in bot]

        for i, (_cs, ce) in enumerate(caller):
            nxt = caller[i + 1] if i + 1 < len(caller) else None
            # When did we next start talking after he stopped?
            after = [b for b in bot_starts if b >= ce]
            if not after:
                continue
            b0 = after[0]
            gap = b0 - ce
            if gap > 4.0:
                continue                      # a different beat entirely
            waits.append(gap)
            if nxt is None:
                continue
            resume_gap = nxt[0] - ce
            if resume_gap > RESUME_S:
                continue                      # he really had finished
            opportunities += 1
            # We started inside the pause, and he carried on after.
            if b0 < nxt[0] and (nxt[1] - nxt[0]) >= MIN_RESUME_S:
                cutoffs += 1

    waits.sort()

    def q(p):
        return waits[int(len(waits) * p)] if waits else 0.0

    return {"calls": calls, "opp": opportunities, "cut": cutoffs,
            "rate": 100.0 * cutoffs / max(opportunities, 1),
            "n_wait": len(waits), "p10": q(0.10), "p50": q(0.50),
            "p90": q(0.90),
            "spread": q(0.90) - q(0.10)}


def main() -> int:
    results = {}
    for name, (cd, bd) in ERAS.items():
        if not cd.exists() or not bd.exists():
            print(f"{name}: no recordings on disk ({cd})")
            continue
        print(f"scoring {name} ...", flush=True)
        results[name] = score(cd, bd)

    if not results:
        return 1

    print("\n\n=== DID IT CUT HIM OFF? =========================================")
    print("he paused briefly, we started talking inside the pause, he carried on\n")
    print(f"{'detector':22} {'calls':>6} {'pauses':>8} {'cut in':>8} {'CUT-OFF RATE':>14}")
    print("-" * 62)
    for name, r in results.items():
        print(f"{name:22} {r['calls']:>6} {r['opp']:>8} {r['cut']:>8} "
              f"{r['rate']:>13.1f}%")

    print("\n\n=== IS IT JUST A FIXED TIMER? ===================================")
    print("silence between his speech ending and ours starting.")
    print("a fixed timer gives a narrow spread; a real decision gives a wide one\n")
    print(f"{'detector':22} {'n':>6} {'p10':>7} {'p50':>7} {'p90':>7} {'spread':>8}")
    print("-" * 62)
    for name, r in results.items():
        print(f"{name:22} {r['n_wait']:>6} {r['p10']:>6.2f}s {r['p50']:>6.2f}s "
              f"{r['p90']:>6.2f}s {r['spread']:>7.2f}s")

    print("\nObservational, not controlled: the two eras are different calls and")
    print("different callers. It is the only direct evidence of live behaviour")
    print("that exists, because no recording of our detector was ever kept until")
    print("today.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
