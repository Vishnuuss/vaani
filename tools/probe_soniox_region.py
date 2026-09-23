"""Compare Soniox's global host against the India region on the SAME audio.

Why this exists
---------------
Ears-only Soniox measured 0.686s STT against Sarvam's 0.392s on 19 Sep, and the
memo written that day named the one remaining lever: `stt-rt.in.soniox.com`,
the India region, which needs a region-specific project key. A key for it
arrived on 23 Sep. STT is an ORG-level setting here, so a live config write
moves all five agents at once -- this proves the key out and prices the region
before anything on the server is touched.

The metric: FLUSH
-----------------
In `stt-rt-v5` Soniox's own endpointing is off, so nothing is ever marked final
until the client sends `{"type":"finalize"}`. That is precisely how the live
pipeline uses it: Silero calls the turn, the service sends finalize, and the
turn cannot proceed until the final tokens come back. That wait IS the 0.686s
measured on 19 Sep, and it is round-trip-dominated, which is what a region
changes.

So: stream at wall clock and send finalize every `--every` seconds, exactly as
a caller pausing would. Flush = finalize sent -> `finished`-for-that-flush
tokens arrive.

An earlier version of this probe timed each final token against its own
`end_ms` audio position. That number was 9.8s and it was an artefact: with
endpointing off, EVERY token finalises in the closing flush, so the metric was
measuring how far back in the clip the words were, not any latency.

Region keys are region-scoped: the India key returns 401 on the global host and
vice versa, so each host is probed with its own key.

    python tools/probe_soniox_region.py --calls 3
    python tools/probe_soniox_region.py --india-key snx_proj_...
"""

from __future__ import annotations

import argparse
import asyncio
import audioop
import json
import statistics as st
import sys
import time
import wave
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

import websockets

HOSTS = {
    "global": "wss://stt-rt.soniox.com/transcribe-websocket",
    "india": "wss://stt-rt.in.soniox.com/transcribe-websocket",
}
AUDIO = Path(".tmp/audio/caller")
CHUNK_MS = 100
RATE = 16000


def env(name: str, default: str = "") -> str:
    for line in Path(".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith(name + "=") and not line.startswith("#"):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return default


def load(path: Path) -> bytes:
    with wave.open(str(path), "rb") as w:
        rate, width, chans = w.getframerate(), w.getsampwidth(), w.getnchannels()
        pcm = w.readframes(w.getnframes())
    if chans > 1:
        pcm = audioop.tomono(pcm, width, 1, 0)
    if width != 2:
        pcm = audioop.lin2lin(pcm, width, 2)
    if rate != RATE:
        pcm, _ = audioop.ratecv(pcm, 2, 1, rate, RATE, None)
    return pcm


def loudest_window(pcm: bytes, seconds: float) -> bytes:
    """The corpus is whole call legs and the caller is silent for much of one.
    Ten seconds of silence returns zero tokens and measures nothing, so take the
    densest window instead of the opening."""
    span = int(RATE * 2 * seconds)
    if len(pcm) <= span:
        return pcm
    step = RATE * 2  # 1s hops
    best, best_i = -1, 0
    for i in range(0, len(pcm) - span, step):
        r = audioop.rms(pcm[i:i + span], 2)
        if r > best:
            best, best_i = r, i
    return pcm[best_i:best_i + span]


async def run(host: str, key: str, pcm: bytes, every: float) -> tuple[float, list[float], int]:
    step = int(RATE * 2 * CHUNK_MS / 1000)
    flushes: list[float] = []
    words = 0

    t_open = time.perf_counter()
    async with websockets.connect(HOSTS[host], max_size=None, open_timeout=25) as ws:
        await ws.send(json.dumps({
            "api_key": key,
            "model": "stt-rt-v5",
            "audio_format": "pcm_s16le",
            "sample_rate": RATE,
            "num_channels": 1,
            "language_hints": ["te", "en"],
        }))
        handshake = time.perf_counter() - t_open
        pending: list[float] = []          # finalize send times, oldest first
        stop = asyncio.Event()

        async def sender():
            start = time.perf_counter()
            nxt = every
            for i in range(0, len(pcm), step):
                due = start + (i / step) * CHUNK_MS / 1000
                d = due - time.perf_counter()
                if d > 0:
                    await asyncio.sleep(d)
                await ws.send(pcm[i:i + step])
                if time.perf_counter() - start >= nxt:
                    pending.append(time.perf_counter())
                    await ws.send(json.dumps({"type": "finalize"}))
                    nxt += every
            pending.append(time.perf_counter())
            await ws.send(json.dumps({"type": "finalize"}))
            await asyncio.sleep(3)
            stop.set()

        async def receiver():
            nonlocal words
            while not stop.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=5)
                except asyncio.TimeoutError:
                    continue
                except websockets.ConnectionClosed:
                    return
                m = json.loads(raw)
                if m.get("error_code"):
                    if m.get("error_code") == 408 and flushes:
                        return
                    raise RuntimeError(f"{m['error_code']} {m.get('error_message')}")
                now = time.perf_counter()
                # Soniox marks the end of a flush with a <fin> token. That is
                # the moment the pipeline is unblocked, so it is what we time.
                for t in m.get("tokens") or []:
                    if t.get("is_final"):
                        words += 1
                    if t.get("text") == "<fin>" and pending:
                        flushes.append(now - pending.pop(0))

        await asyncio.gather(sender(), receiver())
    return handshake, flushes, words


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--calls", type=int, default=3)
    ap.add_argument("--every", type=float, default=4.0,
                    help="seconds between simulated speech-end flushes")
    ap.add_argument("--seconds", type=float, default=30.0)
    ap.add_argument("--india-key")
    ap.add_argument("--global-key")
    a = ap.parse_args()
    keys = {
        "global": a.global_key or env("SONIOX_API_KEY"),
        "india": a.india_key or env("SONIOX_API_KEY_IN") or env("SONIOX_API_KEY"),
    }

    wavs = sorted(AUDIO.glob("wf2_*.wav"))[: a.calls] or sorted(AUDIO.glob("*.wav"))[: a.calls]
    clips = [loudest_window(load(w), a.seconds) for w in wavs]
    print(f"{len(clips)} clips x {a.seconds:.0f}s: {[w.name for w in wavs]}\n")

    for host, key in keys.items():
        if not key:
            print(f"{host:7} no key")
            continue
        hs, lags, words = [], [], 0
        for clip in clips:
            try:
                h, l, n = await run(host, key, clip, a.every)
            except Exception as e:
                print(f"{host:7} key ...{key[-6:]}  FAILED: {e}")
                break
            hs.append(h); lags += l; words += n
        else:
            def p(v, q):
                return f"{st.quantiles(v, n=100)[q - 1]:.3f}s" if len(v) > 2 else (
                    f"{st.median(v):.3f}s" if v else "-")
            print(f"{host:7} key ...{key[-6:]}  handshake {st.median(hs):.3f}s  "
                  f"FLUSH p50 {p(lags, 50)}  p90 {p(lags, 90)}  (n={len(lags)}, {words} words)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
