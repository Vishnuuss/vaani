#!/usr/bin/env python
"""Fetch the run RECORDS for recordings we already hold, so replay can use them.

`harvest_vaani_audio.py` pulls the two audio legs. `replay_turns.replay()` needs
one more thing: the run's `logs.realtime_feedback_events`, which is where the
final caller transcripts live and therefore what `transcripts(run)` reads. The
recordings alone are not replayable without it.

Why this is worth a tool
------------------------
Every cut-off number this project has ever quoted came from `.tmp/audio/caller`,
and 651 of those 654 recordings are dated 2026-08-28 -- the day the Telugu
detector went on the call path. So the corpus predates the thing it is used to
judge, which `harvest_vaani_audio.py` says in its own docstring and which was
still true a month later. Fresh audio arrived on 23 Sep; this closes the gap so
the sweep can read it.

Writes `.tmp/harvest/runs/<workflow>_<run>.json`, the same path and shape the
existing tools already expect, and skips anything already on disk.

    python tools/fetch_run_logs.py --dir .tmp/audio_new/caller
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))
import vaani_runs as V  # noqa: E402

OUT = REPO / ".tmp" / "harvest" / "runs"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=".tmp/audio_new/caller")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()

    wavs = sorted((REPO / a.dir).glob("*.wav"))
    if a.limit:
        wavs = wavs[: a.limit]
    OUT.mkdir(parents=True, exist_ok=True)

    got = skipped = failed = empty = 0
    for w in wavs:
        try:
            wf, rid = w.stem.split("_run")        # wf<W>_run<R>
            wf = wf[2:]
        except ValueError:
            continue
        dest = OUT / f"{wf}_{rid}.json"
        if dest.exists():
            skipped += 1
            continue
        try:
            run = V.get(f"/api/v1/workflow/{wf}/runs/{rid}")
        except Exception as exc:
            print(f"  {w.name}: {type(exc).__name__}")
            failed += 1
            continue
        events = (run.get("logs") or {}).get("realtime_feedback_events") or []
        if not events:
            # Nothing to replay against. Written anyway would make the sweep
            # count a call with zero bursts as a clean one, which flatters
            # every row equally and means nothing.
            empty += 1
            continue
        dest.write_text(json.dumps(run), encoding="utf-8")
        got += 1
        if got % 10 == 0:
            print(f"  {got} fetched")

    print(f"\nfetched {got}  already had {skipped}  no events {empty}  failed {failed}")
    print(f"-> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
