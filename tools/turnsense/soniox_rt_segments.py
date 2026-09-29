#!/usr/bin/env python
"""What production's STT would have handed the turn detector, at every pause.

Production runs Soniox `stt-rt-v5` with its own endpointing OFF: Silero decides
the caller has stopped, pipecat sends `{"type":"finalize"}`, and Soniox returns
the final tokens for everything heard so far, closed by a `<fin>` token
(`service_factory.py`, `vad_force_turn_endpoint=True`). That text is the ONLY
text the turn detector ever sees at decision time.

This replays each caller leg through the same model on the same India host, and
finalizes at exactly the moments production's Silero would have -- the stops in
`replay_eval.prepare()`'s latch. So every pause gets the text as it really would
have been at that moment: real-time punctuation, no hindsight. (Soniox's async
model punctuates with the whole file in view, so it knows whether the caller
carried on; training on its commas and full stops would leak the label.)

Audio is sent faster than real time. That changes nothing about the text: a
finalize covers exactly the audio sent before it, whatever the pace.

    python tools/turnsense/soniox_rt_segments.py --dir .tmp/audio_new/caller
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import wave
from pathlib import Path

import websockets

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tools" / "turnsense"))
import replay_eval as RE  # noqa: E402

OUT = REPO / ".tmp" / "turnsense" / "rt"
URL = "wss://stt-rt.in.soniox.com/transcribe-websocket"
CHUNK_MS = 100


def _key() -> str:
    for line in (REPO / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("SONIOX_API_KEY="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("SONIOX_API_KEY missing from .env")


async def stream(stem: str, wav: Path, log: Path, key: str, speed: float) -> dict:
    d = RE.prepare((stem, str(wav), str(log)))
    segs = [tuple(s) for s in d["segments"]]
    with wave.open(str(wav)) as w:
        sr = w.getframerate()
        pcm = w.readframes(w.getnframes())
    cfg = {"api_key": key, "model": "stt-rt-v5", "audio_format": "pcm_s16le",
           "sample_rate": sr, "num_channels": 1,
           "language_hints": ["te", "en"], "enable_endpoint_detection": False}
    out = {"stem": stem, "segments": [], "err": None}
    if not segs:
        return out
    finals: list[dict] = []
    marks: list[int] = []          # len(finals) at each <fin>, in order

    async with websockets.connect(URL, max_size=None, open_timeout=30,
                                  close_timeout=5) as ws:
        await ws.send(json.dumps(cfg))

        async def reader():
            try:
                async for raw in ws:
                    msg = json.loads(raw) if isinstance(raw, str) else {}
                    if msg.get("error_code"):
                        out["err"] = f"{msg.get('error_code')} {msg.get('error_message')}"
                        return
                    for tok in msg.get("tokens") or []:
                        if not tok.get("is_final"):
                            continue
                        if tok.get("text") == "<fin>":
                            marks.append(len(finals))
                        elif tok.get("text") != "<end>":
                            finals.append(tok)
                    if msg.get("finished"):
                        return
            except websockets.ConnectionClosed:
                return

        rt = asyncio.create_task(reader())
        step = int(sr * CHUNK_MS / 1000) * 2
        pos = 0
        # Messages on one socket are processed in order, so a finalize sent
        # after the audio up to Silero's stop covers exactly that audio. No
        # per-pause waiting: Soniox runs at about real time and waiting on each
        # <fin> let a late one be credited to the next pause.
        for onset, quiet in segs:
            end = min(len(pcm), int(quiet * sr) * 2)
            while pos < end:
                if out["err"]:
                    raise RuntimeError(out["err"])
                nxt = min(end, pos + step)
                await ws.send(pcm[pos:nxt])
                pos = nxt
                await asyncio.sleep(CHUNK_MS / 1000 / speed)
            await ws.send(json.dumps({"type": "finalize"}))
        try:
            await ws.send("")          # pipecat ends a Soniox stream with an EMPTY TEXT frame
        except websockets.ConnectionClosed:
            pass
        try:
            await asyncio.wait_for(rt, timeout=max(60.0, len(pcm) / (2 * sr)))
        except asyncio.TimeoutError:
            rt.cancel()
            out["err"] = out["err"] or "drain timeout"
    prev = 0
    for k, (onset, quiet) in enumerate(segs):
        if k >= len(marks):
            out["err"] = out["err"] or f"only {len(marks)} of {len(segs)} <fin>"
            break
        new = finals[prev:marks[k]]
        prev = marks[k]
        out["segments"].append({
            "onset": onset, "quiet": quiet,
            "text": "".join(t.get("text", "") for t in new).strip(),
            "tokens": [(t.get("text"), t.get("start_ms"), t.get("end_ms"))
                       for t in new]})
    return out


async def main_async(a) -> int:
    key = _key()
    OUT.mkdir(parents=True, exist_ok=True)
    jobs = [j for j in RE.calls(REPO / a.dir) if not (OUT / f"{j[0]}.json").exists()]
    if a.limit:
        jobs = jobs[: a.limit]
    print(f"{len(jobs)} calls to stream at {a.speed}x", flush=True)
    RE.prepare_all(jobs, 10)
    sem = asyncio.Semaphore(a.workers)
    stats = {"ok": 0, "fail": 0}
    t0 = time.time()

    async def one(job):
        stem, wav, log = job
        async with sem:
            for attempt in range(3):
                try:
                    res = await stream(stem, wav, log, key, a.speed)
                    if res.get("err") and attempt < 2:
                        await asyncio.sleep(2)
                        continue
                    (OUT / f"{stem}.json").write_text(
                        json.dumps(res, ensure_ascii=False), encoding="utf-8")
                    stats["ok" if not res.get("err") else "fail"] += 1
                    if res.get("err"):
                        print(f"  ERR {stem}: {res['err'][:150]}", flush=True)
                    break
                except Exception as e:                      # noqa: BLE001
                    if "concurrent" in str(e).lower() or "limit" in str(e).lower():
                        await asyncio.sleep(10)
                    if attempt == 2:
                        stats["fail"] += 1
                        print(f"  FAIL {stem}: {type(e).__name__} {str(e)[:150]}",
                              flush=True)
                    await asyncio.sleep(2)
            n = stats["ok"] + stats["fail"]
            if n % 20 == 0:
                print(f"  {n}/{len(jobs)} ok {stats['ok']} fail {stats['fail']} "
                      f"{time.time() - t0:.0f}s", flush=True)

    await asyncio.gather(*(one(j) for j in jobs))
    print(f"done ok {stats['ok']} fail {stats['fail']} in {time.time() - t0:.0f}s")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=".tmp/audio_new/caller")
    ap.add_argument("--speed", type=float, default=4.0)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--limit", type=int)
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
