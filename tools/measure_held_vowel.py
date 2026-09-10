#!/usr/bin/env python
"""Does a HELD vowel at the end of a burst predict that he is not finished?

The complaint this is chasing
------------------------------
"if i lag more like ahhhhhhhhhhhhh 50000 like that is not waiting, it should
wait." A caller drags out a sound while he thinks, and the agent starts
talking.

Why the existing machinery cannot catch it
-------------------------------------------
`completeness.HESITATIONS` already knows "ఆ" and "hmm" are stalling noises --
but it reads TEXT, and production runs `saarika:v2.5`, which emits no interim
transcripts. No text exists until after the caller has stopped, which is after
the decision has been made.

The 16 prosody features cannot see it either. They describe how energy and
pitch MOVE -- slopes, ranges, a spectral centroid over the last half second.
A three-second "aaaahhhh" is long, steady, voiced and level, which reads as a
calm finished sentence in every one of them. And because it is long, the
fragment floor -- the one guard that buys extra patience -- does not apply:
`_bar()` only raises the bar when `_speech_secs < fragment_secs`. The single
case that most obviously needs more patience is the case that asks for least.

What a filled pause actually is, acoustically
----------------------------------------------
Not a word. A **steady state**. The speaker parks the articulators and holds
one vowel: pitch stops moving, the formants stop moving, energy stops moving.
Real speech does the opposite -- it is continuous change, because that is what
carries the phonemes.

So the measure here is *stationarity*, not identity. It never asks "was that an
aaa or an ooo", which would be a word list wearing a costume and would break on
the next caller. It asks whether the last stretch before the pause carried any
information at all:

    voiced_frac   is it a vowel being held, rather than silence or a consonant
    f0_flat       does the pitch stop moving (a filled pause is monotone)
    spec_flat     do the formants stop moving (low spectral flux = one vowel)
    e_flat        does the loudness stop moving

`held` is all four together, over at least MIN_HOLD_S. Requiring all four is
what keeps a genuine long final syllable -- which falls in pitch and fades in
energy -- from being mistaken for a stall.

What this script decides
------------------------
Whether the signal is real before any of it reaches the agent. It scores every
labelled burst in `turnstops_real.jsonl` and reports how often a held tail
appears on bursts that WERE the end of a turn against bursts that were not. If
the two rates are the same, this idea is wrong and should be dropped.

    python tools/measure_held_vowel.py [--limit N]
"""
from __future__ import annotations

import argparse
import json
import sys
import wave
from collections import defaultdict
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))

CALLER = REPO / ".tmp" / "audio" / "caller"
LABELS = REPO / ".tmp" / "harvest" / "turnstops_real.jsonl"

# The tail actually inspected. Long enough that a normal final syllable cannot
# fill it, short enough that a stall of a second is caught.
TAIL_S = 0.50
MIN_HOLD_S = 0.35

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")


def _f0(x: np.ndarray, sr: int, hop: int) -> np.ndarray:
    """Crude autocorrelation pitch, one value per hop. 0 means unvoiced.

    Deliberately the same shape of estimator `telugu_turn._f0_track` uses --
    the point is to add a feature to that world, not to introduce a second and
    differently-wrong idea of what pitch is.
    """
    lo, hi = int(sr / 400), int(sr / 60)          # 60-400 Hz
    out = []
    win = hop * 2
    for i in range(0, max(0, len(x) - win), hop):
        seg = x[i:i + win].astype(np.float64)
        seg = seg - seg.mean()
        n = np.sqrt((seg ** 2).sum())
        if n < 1e-6:
            out.append(0.0)
            continue
        ac = np.correlate(seg, seg, mode="full")[len(seg) - 1:]
        if len(ac) <= hi:
            out.append(0.0)
            continue
        band = ac[lo:hi]
        k = int(np.argmax(band)) + lo
        out.append(sr / k if ac[k] > 0.35 * ac[0] else 0.0)
    return np.asarray(out, dtype=np.float32)


def held_tail(x: np.ndarray, sr: int) -> dict:
    """Measure how STATIONARY the last TAIL_S of this burst is."""
    tail = x[-int(TAIL_S * sr):]
    if tail.size < int(MIN_HOLD_S * sr):
        return {"held": False}

    hop = max(64, sr // 100)                       # 10 ms
    frame = hop * 2

    e = np.asarray([float(np.sqrt((tail[i:i + frame] ** 2).mean()))
                    for i in range(0, max(0, len(tail) - frame), hop)])
    if e.size < 8:
        return {"held": False}
    e = e / (e.max() + 1e-9)

    f0 = _f0(tail, sr, hop)
    voiced = f0[f0 > 0]
    voiced_frac = float((f0 > 0).mean())

    # Spectral flux: how much the shape of the spectrum changes hop to hop.
    # One held vowel barely changes; running speech changes constantly.
    spec = []
    for i in range(0, max(0, len(tail) - frame), hop):
        s = np.abs(np.fft.rfft(tail[i:i + frame] * np.hanning(frame)))
        spec.append(s / (s.sum() + 1e-9))
    flux = 0.0
    if len(spec) >= 2:
        flux = float(np.mean([np.abs(spec[i] - spec[i - 1]).sum()
                              for i in range(1, len(spec))]))

    f0_cv = float(voiced.std() / (voiced.mean() + 1e-9)) if voiced.size >= 4 else 1.0
    e_cv = float(e.std() / (e.mean() + 1e-9))

    held = (voiced_frac >= 0.70      # a vowel is being held, not silence
            and f0_cv <= 0.06        # pitch has stopped moving
            and flux <= 0.11         # the formants have stopped moving
            and e_cv <= 0.35)        # loudness has stopped moving
    return {"held": held, "voiced_frac": voiced_frac, "f0_cv": f0_cv,
            "flux": flux, "e_cv": e_cv}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int)
    ap.add_argument("--dump", action="store_true",
                    help="print the four numbers for each held tail found")
    a = ap.parse_args()

    rows = [json.loads(line) for line in LABELS.open(encoding="utf-8")]
    by_call: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_call[f"wf{r['workflow']}_run{r['run']}"].append(r)

    stems = sorted(by_call)
    if a.limit:
        stems = stems[: a.limit]

    n = {True: 0, False: 0}
    held = {True: 0, False: 0}
    scanned = 0

    for stem in stems:
        wav = CALLER / f"{stem}.wav"
        if not wav.exists():
            continue
        with wave.open(str(wav)) as w:
            sr = w.getframerate()
            x = np.frombuffer(w.readframes(w.getnframes()),
                              dtype=np.int16).astype(np.float32) / 32768.0
        scanned += 1
        for r in by_call[stem]:
            seg = x[int(r["start"] * sr): int(r["end"] * sr)]
            if seg.size < int(MIN_HOLD_S * sr):
                continue
            m = held_tail(seg, sr)
            end = bool(r["was_turn_end"])
            n[end] += 1
            if m["held"]:
                held[end] += 1
                if a.dump:
                    print(f"  {stem} {r['start']:7.2f}s end={end} "
                          f"voiced={m['voiced_frac']:.2f} f0cv={m['f0_cv']:.3f} "
                          f"flux={m['flux']:.3f} ecv={m['e_cv']:.2f}")
        if scanned % 40 == 0:
            print(f"  {scanned} calls", flush=True)

    print(f"\n{scanned} calls, {n[True] + n[False]} bursts scanned\n")
    print(f"{'burst':30} {'n':>6} {'held tail':>12}")
    print("-" * 52)
    for end, name in ((False, "NOT a turn end (kept going)"),
                      (True, "was a turn end (finished)")):
        pct = 100.0 * held[end] / max(n[end], 1)
        print(f"{name:30} {n[end]:>6} {held[end]:>5} ({pct:>4.1f}%)")

    r_not = held[False] / max(n[False], 1)
    r_end = held[True] / max(n[True], 1)
    print()
    if r_end == 0:
        print("No held tails on finished turns at all.")
    else:
        print(f"A held tail is {r_not / r_end:.2f}x more likely on a burst the "
              "caller CONTINUED.")
    print("\nAbove ~1.5x this is worth feeding to the endpoint as extra")
    print("patience. At ~1.0x it predicts nothing and should be dropped rather")
    print("than shipped because it sounds plausible.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
