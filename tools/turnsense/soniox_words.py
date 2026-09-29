#!/usr/bin/env python
"""Word-level timestamps for every caller recording, from Soniox async (India host).

Why
---
The run logs hold one transcript per TURN AS THE SYSTEM SAW IT: consecutive
logged finals are never less than 0.75 s apart, because any pause shorter than
the 0.7 s timer was merged into one final. So the logs cannot say what the caller
had said at a SHORT pause -- which is exactly the moment the turn detector
decides, and exactly where it cuts people off.

Word timings recover it: at every pause Silero reports, the words spoken so far
are known, and whether the caller carried on is read off the audio. One row per
real decision, labelled by what the caller actually did.

The key in .env is scoped to the India region (memory: soniox-india-region-works):
it 401s on api.soniox.com and 200s on api.in.soniox.com, so the host is fixed
here rather than guessed. Each upload and transcription is deleted once read.

    python tools/turnsense/soniox_words.py --dir .tmp/audio/caller
    python tools/turnsense/soniox_words.py --dir .tmp/audio_new/caller
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

REPO = Path(__file__).resolve().parents[2]
OUT = REPO / ".tmp" / "turnsense" / "words"
HOST = "https://api.in.soniox.com"
MODEL = "stt-async-v5"


def _key() -> str:
    for line in (REPO / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("SONIOX_API_KEY="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("SONIOX_API_KEY missing from .env")


def transcribe(wav: Path, key: str, context: str) -> dict:
    h = {"Authorization": f"Bearer {key}"}
    s = requests.Session()
    with wav.open("rb") as fh:
        r = s.post(f"{HOST}/v1/files", headers=h,
                   files={"file": (wav.name, fh, "audio/wav")}, timeout=120)
    r.raise_for_status()
    file_id = r.json()["id"]
    tid = None
    try:
        body = {"model": MODEL, "file_id": file_id,
                "language_hints": ["te", "en"], "context": context}
        r = s.post(f"{HOST}/v1/transcriptions", headers=h, json=body, timeout=60)
        if r.status_code >= 400:
            raise RuntimeError(f"create {r.status_code}: {r.text[:300]}")
        tid = r.json()["id"]
        for _ in range(600):
            r = s.get(f"{HOST}/v1/transcriptions/{tid}", headers=h, timeout=30)
            r.raise_for_status()
            st = r.json().get("status")
            if st == "completed":
                break
            if st == "error":
                raise RuntimeError(f"transcription error: {r.json()}")
            time.sleep(1.0)
        else:
            raise RuntimeError("timed out waiting for transcription")
        r = s.get(f"{HOST}/v1/transcriptions/{tid}/transcript", headers=h, timeout=60)
        r.raise_for_status()
        return r.json()
    finally:
        if tid:
            s.delete(f"{HOST}/v1/transcriptions/{tid}", headers=h, timeout=30)
        s.delete(f"{HOST}/v1/files/{file_id}", headers=h, timeout=30)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=".tmp/audio/caller")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--limit", type=int)
    a = ap.parse_args()
    key = _key()
    OUT.mkdir(parents=True, exist_ok=True)
    # Domain words Soniox should expect. Context biases recognition; it does not
    # invent speech.
    context = ("Telugu phone call with a loan, solar, property or investment "
               "company. Words: లోన్, సోలార్, సబ్సిడీ, EMI, CIBIL, అపార్ట్‌మెంట్, "
               "విజయవాడ, హైదరాబాద్, లక్షలు, వేలు.")

    wavs = []
    for w in sorted((REPO / a.dir).glob("*.wav")):
        if (OUT / f"{w.stem}.json").exists():
            continue
        with w.open("rb") as fh:
            if fh.read(4) != b"RIFF":
                continue
        wavs.append(w)
    if a.limit:
        wavs = wavs[: a.limit]
    print(f"{len(wavs)} recordings to transcribe", flush=True)
    done = fail = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = {ex.submit(transcribe, w, key, context): w for w in wavs}
        for f in as_completed(futs):
            w = futs[f]
            try:
                res = f.result()
                (OUT / f"{w.stem}.json").write_text(
                    json.dumps(res, ensure_ascii=False), encoding="utf-8")
                done += 1
            except Exception as e:                       # noqa: BLE001
                fail += 1
                print(f"  FAIL {w.stem}: {str(e)[:200]}", flush=True)
            if (done + fail) % 25 == 0:
                print(f"  {done + fail}/{len(wavs)}  ok {done}  fail {fail}  "
                      f"{time.time() - t0:.0f}s", flush=True)
    print(f"done: ok {done}, failed {fail}, {time.time() - t0:.0f}s")
    return 0 if not fail else 1


if __name__ == "__main__":
    raise SystemExit(main())
