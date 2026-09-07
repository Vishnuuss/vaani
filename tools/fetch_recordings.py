"""Download the separated caller recordings the turn detector needs.

READ-ONLY against production: GETs only, and every file is cached, so a rerun
costs nothing and an interrupted download resumes where it stopped.

Why the CALLER track specifically
---------------------------------
`audio_index.jsonl` points at `recordings/<id>/user.wav` -- the caller isolated
from the agent. A turn detector has to answer "has this person finished
speaking?", and on a mixed track the agent's own voice is the loudest thing in
the file. The separated track is the only one where the question is answerable.

Why audio at all, when a text model already exists
--------------------------------------------------
The text classifier reached 9% of turns at a safe threshold and stalls there,
because short Telugu answers are genuinely ambiguous in a transcript: "మా" is a
complete turn in one call and the opening of a sentence in another. Falling
intonation and pause length separate them, and neither survives transcription.
That is why Smart Turn is an audio model -- and why Telugu, which it does not
cover, needs one built.

    python tools/fetch_recordings.py --limit 200
    python tools/fetch_recordings.py            # everything in the index
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")


def fetch(url: str, dest: Path, timeout: int = 120) -> tuple[bool, int]:
    if dest.exists() and dest.stat().st_size > 1000:
        return True, dest.stat().st_size          # cached
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "vaani-harvest/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
        return False, 0
    if len(data) < 1000:
        return False, len(data)                   # an error page, not audio
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return True, len(data)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", default=".tmp/harvest/audio_index.jsonl")
    ap.add_argument("--out")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--leg", choices=("user", "bot"), default="user",
                    help="user = the caller isolated (what the turn detector "
                         "scores). bot = the agent's own track, needed to know "
                         "WHEN WE WERE TALKING -- a backchannel is defined by "
                         "overlap, so it cannot be labelled from the caller "
                         "alone.")
    a = ap.parse_args()
    if not a.out:
        a.out = f".tmp/audio/{a.leg if a.leg == 'bot' else 'caller'}"

    rows = [json.loads(x) for x in
            Path(a.index).read_text(encoding="utf-8").splitlines() if x.strip()]
    if a.limit:
        rows = rows[: a.limit]
    out = Path(a.out)
    print(f"{len(rows)} recording(s) to fetch -> {out}\n", flush=True)

    ok = failed = cached = skipped = 0
    total = 0
    key = "url" if a.leg == "user" else "bot_url"
    for i, row in enumerate(rows, 1):
        url = row.get(key)
        if not url:
            # Index rows written before the bot leg was recorded carry no
            # bot_url. Re-run vaani_harvest.py --audio to refresh the index.
            skipped += 1
            continue
        dest = out / f"wf{row['workflow']}_run{row['run']}.wav"
        existed = dest.exists()
        good, size = fetch(url, dest)
        if good:
            total += size
            if existed:
                cached += 1
            else:
                ok += 1
        else:
            failed += 1
        if i % 25 == 0 or i == len(rows):
            print(f"  {i}/{len(rows)}  new={ok} cached={cached} failed={failed}  "
                  f"{total/1e6:.0f} MB", flush=True)

    print(f"\n{ok} downloaded, {cached} already present, {failed} failed"
          + (f", {skipped} had no {a.leg} url in the index" if skipped else ""))
    print(f"{total/1e6:.0f} MB in {out}")
    if failed:
        print("failures are usually runs whose audio was never written "
              "(calls that did not connect)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
