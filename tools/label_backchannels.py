#!/usr/bin/env python
"""Label the four turn states from the two legs of a real call.

Why the four states, and why they cannot come from the caller alone
--------------------------------------------------------------------
The turn detector answers one bit today: has he finished. That bit cannot
express the two behaviours the agent gets wrong most often on a phone call:

    "ఆ" said while WE are talking          -> keep talking. He is listening.
    "ఆగండి" said while WE are talking      -> stop NOW. He wants the floor.

Both are short. Both overlap our speech. On the caller's track alone they are
the same event, which is why a detector trained on that track alone answered
"turn over" to a thinking noise 113 times in the original training set.

The 2026 work names the same four states -- Easy Turn (ICASSP 2026) predicts
complete / incomplete / backchannel / wait, and FastTurn's head does the same.
What makes them labellable is the AGENT's track: "while we were talking" is only
knowable from our own audio, and this project indexed the caller's URL beside
the bot's and never fetched the bot's.

The rules, and the reasoning behind each
-----------------------------------------
For every caller burst, with `bot` = the agent's speech regions:

  BACKCHANNEL  short, overlaps our speech, and he does NOT go on to take the
               floor. A listener noise. Answering it is the defect.
  WAIT         overlaps our speech and he DOES go on. He wanted the floor and
               took it -- a genuine barge-in.
  INCOMPLETE   no overlap, but he resumes within `RESUME_S`. Nobody answers a
               question, hears a reply begin, and starts a fresh sentence inside
               a second: a resume that fast is the back half of a sentence we
               cut in two.
  COMPLETE     no overlap, and he stayed quiet afterwards.

`RESUME_S` is 1.0s to match `telugu_turn.RESUME_WINDOW_S`, which already uses
exactly this reasoning to decide it interrupted somebody. One definition, not
two.

Honest limits
-------------
This is heuristic labelling from energy, not human annotation. It inherits
whatever `true_latency.regions` gets wrong, and on a speakerphone the caller's
leg carries some of our own voice, which will read as overlap that is really
echo. It is good enough to TRAIN on -- the alternative is no backchannel labels
at all -- and it is not good enough to quote as ground truth.

    python tools/label_backchannels.py [--limit N]
"""
from __future__ import annotations

import argparse
import json
import sys
import wave
from collections import Counter
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))
import true_latency as TL  # noqa: E402

CALLER = REPO / ".tmp" / "audio" / "caller"
BOT = REPO / ".tmp" / "audio" / "bot"
OUT = REPO / ".tmp" / "harvest" / "turnstops_4state.jsonl"

SHORT_S = 1.0          # above this it is a sentence, whatever the words
RESUME_S = 1.0         # matches telugu_turn.RESUME_WINDOW_S -- one definition
FOLLOW_S = 2.0         # how long to look for him taking the floor
FLOOR_S = 0.6          # speech this long after an overlap means he took it

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")


def _speech(path: Path) -> tuple[list[tuple[float, float]], float]:
    with wave.open(str(path)) as w:
        sr = w.getframerate()
        x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32)
    x = x / 32768.0
    return TL.regions(x, sr), len(x) / sr


def _overlaps(a0: float, a1: float, spans) -> float:
    """Seconds of `a` that fall inside any span. Zero means we were silent."""
    return sum(max(0.0, min(a1, s1) - max(a0, s0)) for s0, s1 in spans)


def label_call(stem: str) -> list[dict]:
    caller, _ = _speech(CALLER / f"{stem}.wav")
    bot, _ = _speech(BOT / f"{stem}.wav")
    rows = []
    for i, (s, e) in enumerate(caller):
        dur = e - s
        over = _overlaps(s, e, bot)
        # How much he speaks in the seconds AFTER this burst -- did he take the
        # floor, or was that the whole of it?
        after = sum(min(e2, e + FOLLOW_S) - max(s2, e)
                    for s2, e2 in caller[i + 1:]
                    if s2 < e + FOLLOW_S and e2 > e)
        gap = (caller[i + 1][0] - e) if i + 1 < len(caller) else 99.0

        if over > 0.15 * dur:                       # he spoke over us
            state = "wait" if after >= FLOOR_S else "backchannel"
            if dur > SHORT_S and after < FLOOR_S:
                # Long, over our speech, and then nothing. Not a listener noise
                # -- he said something and we talked through it.
                state = "wait"
        elif gap < RESUME_S:
            state = "incomplete"
        else:
            state = "complete"

        rows.append({"call": stem, "start": round(s, 2), "end": round(e, 2),
                     "dur": round(dur, 2), "overlap": round(over, 2),
                     "after": round(after, 2), "gap": round(min(gap, 99.0), 2),
                     "state": state})
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int)
    a = ap.parse_args()

    stems = sorted({p.stem for p in CALLER.glob("*.wav")} &
                   {p.stem for p in BOT.glob("*.wav")})
    if a.limit:
        stems = stems[: a.limit]
    print(f"{len(stems)} calls with BOTH legs on disk\n")

    rows: list[dict] = []
    for n, stem in enumerate(stems, 1):
        try:
            rows.extend(label_call(stem))
        except Exception as e:
            print(f"  skip {stem}: {e.__class__.__name__}")
        if n % 40 == 0:
            print(f"  {n}/{len(stems)}", flush=True)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    c = Counter(r["state"] for r in rows)
    print(f"\n{len(rows)} labelled bursts -> {OUT.name}\n")
    for state in ("complete", "incomplete", "backchannel", "wait"):
        n = c[state]
        print(f"  {state:12} {n:5}  {100.0*n/max(len(rows),1):5.1f}%")

    bc = [r for r in rows if r["state"] == "backchannel"]
    if bc:
        d = sorted(r["dur"] for r in bc)
        print(f"\n  backchannel duration  p50 {d[len(d)//2]:.2f}s  "
              f"p90 {d[int(len(d)*0.9)]:.2f}s")
        print("  (the 0.35s barge-in floor should sit under this, not over it)")
    print("\nHeuristic labels from energy, not human annotation. Good enough to")
    print("TRAIN on -- the alternative is no backchannel labels at all -- and")
    print("not good enough to quote as ground truth.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
