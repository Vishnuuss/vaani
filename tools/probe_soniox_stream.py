"""Soniox turn detection on a WHOLE call, streamed continuously.

Why this replaces probe_soniox.py's verdict
--------------------------------------------
The first probe cut each labelled burst into its own websocket session and
closed the socket when the clip ran out. Both choices were wrong and they broke
the number that mattered:

  1. Closing a Soniox stream FORCES finalisation and emits `<end>`. On a
     mid-turn burst the clip ends exactly where the caller was about to carry
     on, so the socket closed there and the forced flush was counted as "it cut
     him off". That inflated false cutoffs, and it inflated them ONLY on the
     mid-turn class -- precisely the class the verdict rested on.
  2. Soniox's endpointing is documented as semantic: tone, meaning and
     conversational flow. Feeding it 2-4 second fragments with no history strips
     out the input it runs on. Nobody would judge the Telugu detector by showing
     it a sentence with the first half deleted.

This project has made this exact class of error before and written it down:
`compare_detectors.py` records that "every `replay_turns.py` cut-off number was
produced by replaying PRE-28-AUGUST audio", and that "counting raw overlap
between the two legs gave 76% and is echo". A measurement artefact that flatters
the conclusion you already hold is the standing hazard here.

So: one session per call, the caller's whole leg streamed at wall-clock speed,
never closed until the call ends. Every `<end>` Soniox emits is logged with its
audio-domain timestamp, then scored against the hand-labelled bursts:

    label was_turn_end=True   an `<end>` shortly after the burst ends is a HIT,
                              and the delay is the wait a caller would feel
    label was_turn_end=False  an `<end>` inside the gap BEFORE the caller
                              resumes is a FALSE CUTOFF

    python tools/probe_soniox_stream.py --calls 8
    python tools/probe_soniox_stream.py --calls 8 --sensitivity -0.5
"""

from __future__ import annotations

import argparse
import asyncio
import audioop
import collections
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

URL = "wss://stt-rt.soniox.com/transcribe-websocket"
CORPUS = Path(".tmp/harvest/turnstops_real.jsonl")
AUDIO = Path(".tmp/audio/caller")
CHUNK_MS = 100
END_TOKENS = ("<end>", "<fin>")

# How long after a labelled burst ends an `<end>` still counts as belonging to
# it. Beyond this the endpoint is attributed to nothing and reported separately.
ATTRIB_S = 2.5


def env(name: str) -> str:
    for line in Path(".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith(name + "=") and not line.startswith("#"):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit(name + " not in .env")


def load_call(path: Path):
    with wave.open(str(path), "rb") as w:
        rate, width, chans = w.getframerate(), w.getsampwidth(), w.getnchannels()
        pcm = w.readframes(w.getnframes())
    if chans > 1:
        pcm = audioop.tomono(pcm, width, 0.5, 0.5)
    if width != 2:
        pcm = audioop.lin2lin(pcm, width, 2)
    return pcm, rate


async def stream_call(key: str, path: Path, opts: dict):
    """Stream one entire caller leg. Return every endpoint, in audio seconds."""
    pcm, rate = load_call(path)
    cfg = {
        "api_key": key,
        "model": opts["model"],
        "audio_format": "pcm_s16le",
        "sample_rate": rate,
        "num_channels": 1,
        "enable_endpoint_detection": True,
        "language_hints": ["te", "en"],
    }
    for k, ck in (("max_delay", "max_endpoint_delay_ms"),
                  ("sensitivity", "endpoint_sensitivity"),
                  ("level", "endpoint_latency_adjustment_level")):
        if opts.get(k) is not None:
            cfg[ck] = opts[k]

    endpoints = []          # audio seconds at which <end> was emitted
    words = []              # (audio_s, text) for every real final token
    err = None
    try:
        async with websockets.connect(URL, max_size=None, open_timeout=25,
                                      close_timeout=5) as ws:
            await ws.send(json.dumps(cfg))
            sent_s = {"v": 0.0}
            finished = asyncio.Event()

            async def send():
                step = int(rate * CHUNK_MS / 1000) * 2
                for i in range(0, len(pcm), step):
                    if finished.is_set():
                        return
                    await ws.send(pcm[i:i + step])
                    sent_s["v"] += len(pcm[i:i + step]) / (2 * rate)
                    await asyncio.sleep(CHUNK_MS / 1000)
                # Only NOW may the stream close. The whole point of this probe
                # is that it is never closed mid-conversation.
                try:
                    await ws.send(b"")
                except Exception:
                    pass

            task = asyncio.create_task(send())
            try:
                while True:
                    raw = await asyncio.wait_for(ws.recv(), timeout=60)
                    msg = json.loads(raw) if isinstance(raw, str) else {}
                    if msg.get("error_code"):
                        err = str(msg.get("error_message"))[:80]
                        break
                    proc = msg.get("final_audio_proc_ms")
                    for tok in msg.get("tokens", []) or []:
                        txt = tok.get("text", "")
                        if txt in END_TOKENS:
                            if tok.get("is_final") and proc:
                                # Audio-domain. Wall clock drifts ~10% here.
                                endpoints.append(proc / 1000.0)
                            continue
                        if tok.get("is_final") and tok.get("end_ms"):
                            words.append((tok["end_ms"] / 1000.0, txt))
                    if msg.get("finished"):
                        break
            except (asyncio.TimeoutError, websockets.ConnectionClosed):
                pass
            finally:
                finished.set()
                task.cancel()
    except Exception as e:
        err = (type(e).__name__ + ": " + str(e))[:80]
    # The final endpoint is the forced one from closing the stream at the true
    # end of the call. It is not a decision, so it is dropped.
    if endpoints and endpoints[-1] >= (len(pcm) / (2 * rate)) - 0.5:
        endpoints = endpoints[:-1]
    return endpoints, words, err


def score(bursts, endpoints):
    """Attribute each endpoint to a labelled burst and tally the two numbers."""
    hits, waits, cutoffs, missed, orphan = [], [], [], [], []
    used = set()
    for b in bursts:
        end = b["end"]
        gap = b.get("gap_after")
        # Window in which an endpoint belongs to THIS burst.
        if b["was_turn_end"]:
            hi = end + ATTRIB_S if gap is None else end + min(gap, ATTRIB_S)
        else:
            # Only the gap before he resumes. Firing in there is a cut-off.
            hi = end + (ATTRIB_S if gap is None else gap)
        cand = [i for i, e in enumerate(endpoints)
                if i not in used and end <= e <= hi]
        if cand:
            i = cand[0]
            used.add(i)
            delay = endpoints[i] - end
            if b["was_turn_end"]:
                hits.append(b)
                waits.append(delay)
            else:
                cutoffs.append((b, delay))
        elif b["was_turn_end"]:
            missed.append(b)
    orphan = [e for i, e in enumerate(endpoints) if i not in used]
    return hits, waits, cutoffs, missed, orphan


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workflow", default="2")
    ap.add_argument("--calls", type=int, default=8)
    ap.add_argument("--model", default="stt-rt-v5")
    ap.add_argument("--max-delay", type=int, default=None)
    ap.add_argument("--sensitivity", type=float, default=None)
    ap.add_argument("--level", type=int, default=None)
    ap.add_argument("--concurrency", type=int, default=8)
    a = ap.parse_args()

    rows = [json.loads(l) for l in CORPUS.open(encoding="utf-8")]
    rows = [r for r in rows if r["workflow"] == a.workflow]
    by_call = collections.defaultdict(list)
    for r in rows:
        p = AUDIO / ("wf" + a.workflow + "_run" + r["run"] + ".wav")
        if p.exists():
            by_call[r["run"]].append(r)
    # Richest calls first: most labelled bursts per second of audio streamed.
    picked = sorted(by_call.items(), key=lambda kv: -len(kv[1]))[:a.calls]
    total_b = sum(len(v) for _, v in picked)
    print("workflow %s: %d calls, %d labelled bursts (%d ends / %d mid-turn)"
          % (a.workflow, len(picked), total_b,
             sum(1 for _, v in picked for r in v if r["was_turn_end"]),
             sum(1 for _, v in picked for r in v if not r["was_turn_end"])))
    opts = {"model": a.model, "max_delay": a.max_delay,
            "sensitivity": a.sensitivity, "level": a.level}
    print("config: %s\n" % {k: v for k, v in opts.items() if v is not None})

    key = env("SONIOX_API_KEY")
    sem = asyncio.Semaphore(a.concurrency)
    out = {}

    async def work(run, bursts):
        p = AUDIO / ("wf" + a.workflow + "_run" + run + ".wav")
        async with sem:
            t0 = time.monotonic()
            eps, words, err = await stream_call(key, p, opts)
        secs = wave.open(str(p), "rb").getnframes() / 8000.0
        out[run] = (eps, words, err, bursts)
        print("  run%-5s %5.0fs audio  %2d endpoints  %3d words  %s"
              % (run, secs, len(eps), len(words), err or ""))

    async def go():
        await asyncio.gather(*(work(r, b) for r, b in picked))

    asyncio.run(go())

    H = W = []
    hits, waits, cutoffs, missed, orphan = [], [], [], [], []
    for run, (eps, words, err, bursts) in out.items():
        h, w, c, m, o = score(sorted(bursts, key=lambda r: r["start"]), eps)
        hits += h; waits += w; cutoffs += c; missed += m; orphan += o

    ends = [r for _, (_, _, _, bs) in out.items() for r in bs if r["was_turn_end"]]
    mids = [r for _, (_, _, _, bs) in out.items() for r in bs if not r["was_turn_end"]]

    print("\n" + "=" * 68)
    print("CONTINUOUS STREAM  model=%s  %s" % (a.model,
          {k: v for k, v in opts.items() if v is not None and k != "model"}))
    print("=" * 68)
    cr = len(cutoffs) / len(mids) if mids else 0.0
    print("  FALSE CUTOFFS  %d/%d = %.1f%%      bar 2.0%%   "
          "(live detector 6.45%%, dumb 0.6s timer 19.1%%)"
          % (len(cutoffs), len(mids), cr * 100))
    if waits:
        sw = sorted(waits)
        print("  WAIT on real ends  p50 %.3fs  mean %.3fs  p90 %.3fs"
              % (st.median(waits), st.mean(waits),
                 sw[max(0, int(len(sw) * 0.9) - 1)]))
        print("  ends detected  %d/%d = %.0f%%   under 0.6s %d/%d"
              % (len(hits), len(ends), len(hits) / len(ends) * 100,
                 sum(1 for x in waits if x < 0.6), len(waits)))
    print("  missed real ends %d   unattributed endpoints %d"
          % (len(missed), len(orphan)))
    if cutoffs:
        print("\n  cut-offs, delay after he paused (his pause was gap_after):")
        for b, d in sorted(cutoffs, key=lambda x: x[1])[:8]:
            print("    run%-5s fired %.2fs into a %.2fs pause   %s"
                  % (b["run"], d, b.get("gap_after") or 0,
                     (b.get("text") or "")[:34]))
    print("")


if __name__ == "__main__":
    main()
