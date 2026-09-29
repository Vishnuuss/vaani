#!/usr/bin/env python
"""Did OUR turn detector make the agent interrupt more? Measured from the audio.

Why this exists, and what it replaces
--------------------------------------
The client's claim is specific and testable: "turn detection problem has
appeared on the day we built our own turn detector." Our Telugu detector went
on the call path on **2026-08-28** (0ac025d), so there is a clean before and
after.

An earlier attempt to answer this by keyword-matching caller transcripts for
complaints was **wrong and has been retracted**. It counted 27 "complaints" of
which 25 were the identical seeded string "ఆగండి మాట్లాడనివ్వట్లేదు" repeated
across runs 1247-1822 on four different workflows, and 2 were false matches on
the ordinary word "మధ్యలో". Keyword-matching a transcript measures the test
harness, not the caller.

So this measures the physical event instead. No keywords, no labels, no model:

    an interruption is the BOT's audio starting while the CALLER's audio is
    still going, and the caller carrying on afterwards.

Both legs are on disk for every call that has them -- the bot's own track was
indexed in every run log and had to be fetched to make this knowable at all.

The one bias, stated
--------------------
On a speakerphone the caller's leg carries some of our own voice, which reads
as overlap that is really echo. That bias applies EQUALLY to both periods, so
it inflates both rates and leaves the comparison intact. It would only mislead
if speakerphone use changed sharply across 28 August, which nothing suggests.

    python tools/measure_bot_interruptions.py
"""
from __future__ import annotations

import json
import sys
import wave
from collections import defaultdict
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))
import true_latency as TL  # noqa: E402

CALLER = REPO / ".tmp" / "audio" / "caller"
BOT = REPO / ".tmp" / "audio" / "bot"
RUNS = REPO / ".tmp" / "harvest" / "runs"

# 0ac025d "Put a turn detector that speaks Telugu on the call path"
SWITCH = "2026-08-28"

# How much of the caller's burst must remain after we start talking for it to
# count as us cutting in, rather than us starting a hair early on a burst that
# was ending anyway.
STILL_GOING_S = 0.30

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")


def _regions(path: Path):
    with wave.open(str(path)) as w:
        sr = w.getframerate()
        x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    return TL.regions(x.astype(np.float32) / 32768.0, sr)


def _date_for(stem: str) -> str | None:
    """`wf1_run1053` -> the run log's created_at date."""
    try:
        wf, rid = stem.split("_run")
    except ValueError:
        return None
    p = RUNS / f"{wf[2:]}_{rid}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("created_at", "")[:10]
    except Exception:
        return None


def main() -> int:
    stems = sorted({p.stem for p in CALLER.glob("*.wav")} &
                   {p.stem for p in BOT.glob("*.wav")})
    print(f"{len(stems)} calls with BOTH audio legs on disk\n")

    buckets: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])  # calls, bot turns, cut-ins
    detail: dict[str, list[float]] = defaultdict(list)

    for n, stem in enumerate(stems, 1):
        date = _date_for(stem)
        if not date:
            continue
        era = "OURS (28 Aug +)" if date >= SWITCH else "DOGRAH (pre-28 Aug)"
        try:
            caller = _regions(CALLER / f"{stem}.wav")
            bot = _regions(BOT / f"{stem}.wav")
        except Exception:
            continue

        cut = 0
        for bs, _be in bot:
            # Was the caller mid-burst when we started, with real speech left?
            for cs, ce in caller:
                if cs < bs < ce and (ce - bs) >= STILL_GOING_S:
                    cut += 1
                    break

        b = buckets[era]
        b[0] += 1
        b[1] += len(bot)
        b[2] += cut
        if bot:
            detail[era].append(100.0 * cut / len(bot))

        if n % 40 == 0:
            print(f"  {n}/{len(stems)}", flush=True)

    print(f"\n{'era':22} {'calls':>6} {'bot turns':>10} {'cut in':>9} {'rate':>8} "
          f"{'per-call p50':>13}")
    print("-" * 74)
    for era in ("DOGRAH (pre-28 Aug)", "OURS (28 Aug +)"):
        c, t, x = buckets[era]
        if not c:
            print(f"{era:22} no calls")
            continue
        per = sorted(detail[era])
        p50 = per[len(per) // 2] if per else 0.0
        print(f"{era:22} {c:>6} {t:>10} {x:>9} {100.0*x/max(t,1):>7.1f}% "
              f"{p50:>12.1f}%")

    print("\nAn interruption here is a physical event: our audio starting while")
    print("his was still going, with at least 0.30s of his speech left. No")
    print("keywords, no labels, no model. Speakerphone echo inflates BOTH rows")
    print("equally and does not move the comparison.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
