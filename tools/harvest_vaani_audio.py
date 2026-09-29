#!/usr/bin/env python
"""Fetch both audio legs for the calls our OWN turn detector actually ran.

Why this had to be written
---------------------------
Every recording this project holds stops on 2026-08-27. Our Telugu detector
went on the call path on 2026-08-28 (0ac025d). So the entire corpus -- the
2,950 bursts the detector was TRAINED on, and every cut-off number quoted from
`replay_turns.py` -- comes from calls that happened before the detector existed.

That is a fair controlled comparison between detectors on identical audio. It
is not, and never was, evidence about how our detector behaves on a real call.
The client has been on every call since 28 August and says it interrupts him;
there was no recording on disk with which to check.

This fixes that. It pulls the vaani backend's runs from 28 August onward and
saves the caller and bot legs beside the existing corpus, so the same tools
that read `.tmp/audio/{caller,bot}` can read these too.

    python tools/harvest_vaani_audio.py [--since 2026-08-28]
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT_C = REPO / ".tmp" / "audio_new" / "caller"
OUT_B = REPO / ".tmp" / "audio_new" / "bot"
RUNS = REPO / ".tmp" / "runs_new"

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

env = {}
for line in (REPO / ".env").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
BASE, KEY = env["VAANI_SERVER_API_URL"], env["VAANI_SERVER_API_KEY"]


def api(path: str):
    r = urllib.request.Request(f"{BASE}{path}", headers={"X-API-Key": KEY})
    return json.load(urllib.request.urlopen(r, timeout=120))


def fetch(url: str, dest: Path) -> bool:
    if dest.exists() and dest.stat().st_size > 1000:
        return True
    try:
        with urllib.request.urlopen(url, timeout=180) as r:
            data = r.read()
        if len(data) < 1000:
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return True
    except Exception:
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-08-28")
    a = ap.parse_args()

    runs = []
    for wf in range(1, 9):
        for page in (1, 2, 3):
            try:
                d = api(f"/api/v1/workflow/{wf}/runs?page={page}&limit=100")
            except Exception:
                break
            rs = d.get("runs", [])
            if not rs:
                break
            for r in rs:
                if r.get("mode") == "textchat":
                    continue
                if (r.get("created_at") or "")[:10] < a.since:
                    continue
                runs.append((wf, r["id"]))

    print(f"{len(runs)} phone runs since {a.since}\n")
    RUNS.mkdir(parents=True, exist_ok=True)
    both = one = none = 0

    for n, (wf, rid) in enumerate(runs, 1):
        try:
            d = api(f"/api/v1/workflow/{wf}/runs/{rid}")
        except Exception:
            none += 1
            continue
        (RUNS / f"{wf}_{rid}.json").write_text(
            json.dumps(d, ensure_ascii=False), encoding="utf-8")

        stem = f"wf{wf}_run{rid}"
        cu = d.get("user_recording_public_url") or d.get("user_recording_url")
        bu = d.get("bot_recording_public_url") or d.get("bot_recording_url")
        got_c = bool(cu) and fetch(cu, OUT_C / f"{stem}.wav")
        got_b = bool(bu) and fetch(bu, OUT_B / f"{stem}.wav")
        if got_c and got_b:
            both += 1
        elif got_c or got_b:
            one += 1
        else:
            none += 1
        if n % 15 == 0:
            print(f"  {n}/{len(runs)}  both={both} one={one} none={none}", flush=True)

    print(f"\nboth legs : {both}")
    print(f"one leg   : {one}")
    print(f"neither   : {none}")
    print(f"\n-> {OUT_C}")
    print(f"-> {OUT_B}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
