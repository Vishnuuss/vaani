"""Does Soniox's semantic endpoint beat MB Solar's 0.9s blind timer?

Why this runs before any pipeline code
---------------------------------------
Production ends every turn on a stopwatch: Silero VAD at 0.2s plus
`dograh_speech_timeout_secs` at 0.7s. Measured on 11 Sep across 8 calls and 69
turns, 0 of 69 endpoints came in under 0.6s, minimum 0.831s. That 0.9s floor is
58% of run 972's 1.555s turn. It is the largest single number on the project.

Soniox claims semantic endpointing for Telugu -- tone, meaning and speech flow
rather than silence -- and ships an `<end>` token when it decides. If that is
true at 8 kHz on real phone audio, the floor goes away. If it is not, we have
learned it for the price of some API credit and no live calls.

This project has been burned twice believing a vendor latency claim. Sarvam's
realtime model measured a genuine 3x offline and made live calls WORSE, because
its server-side VAD stacked 500ms on top of ours. So this probe measures the
two numbers that actually decide it, and they pull against each other:

    false cutoff rate   of bursts a human labelled NOT a turn end, how many
                        did Soniox end anyway?              <- the cardinal sin
    wait on true ends   of bursts that WERE a turn end, how long after the
                        speech stopped did `<end>` arrive?  <- the reward

That is `model_registry.py`'s promotion rule verbatim: a candidate ships only
if false cutoffs do not get worse AND the wait improves. A model with a better
wait and a worse cutoff rate is a worse model.

The comparison set is fixed in advance, from `turnstops_real.jsonl` -- the
2,950-row hand-corrected corpus, not the poisoned one-class set that taught the
old model that "aa" is a complete sentence. Filtered to workflow 2 that is 194
bursts, 99 real ends and 95 non-ends.

What it must beat, from the same corpus and the stopwatch control:

    SHIPPED detector (live)      6.45% false cutoffs
    retrained linear @0.70       2.05% false cutoffs
    declared safety bar          2.00%
    dumb timer 0.6s              19.1% cut off, wait p50 0.80s
    current live floor           0.9s, nothing under 0.6s

Audio is streamed at WALL-CLOCK speed. Firehosing the file would answer a
different and useless question, because the whole point is whether the decision
arrives before a caller notices the pause.

    python tools/probe_soniox.py --limit 20
    python tools/probe_soniox.py --max-delay 800 --sensitivity 0.3 --level 2
    python tools/probe_soniox.py --sweep
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

URL = "wss://stt-rt.soniox.com/transcribe-websocket"
CORPUS = Path(".tmp/harvest/turnstops_real.jsonl")
AUDIO_DIRS = (Path(".tmp/audio/caller"), Path(".tmp/audio_new/caller"))
CHUNK_MS = 100
END_TOKENS = ("<end>", "<fin>")

# How much of the real following silence to let it hear. Capped so one long
# pause cannot dominate the run, and never padded with synthetic silence:
# artificial quiet would invent false cutoffs that could not happen on a call.
WINDOW_S = 3.0


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


def find_audio(wf, run):
    for d in AUDIO_DIRS:
        p = d / ("wf" + str(wf) + "_run" + str(run) + ".wav")
        if p.exists():
            return p
    return None


def slice_burst(pcm, rate, start, end, gap):
    """Burst audio plus the real gap that followed it, capped."""
    tail = WINDOW_S if gap is None else min(gap, WINDOW_S)
    a = max(0, int(start * rate)) * 2
    b = min(len(pcm) // 2, int((end + tail) * rate)) * 2
    speech_len = max(0.0, end - start)
    return pcm[a:b], speech_len


async def one_burst(key, pcm, rate, speech_s, opts):
    """Stream one burst. Returns when <end> lands or the audio runs out."""
    cfg = {
        "api_key": key,
        "model": opts["model"],
        "audio_format": "pcm_s16le",
        "sample_rate": rate,
        "num_channels": 1,
        "enable_endpoint_detection": True,
        "language_hints": ["te", "en"],
        "max_endpoint_delay_ms": opts["max_delay"],
    }
    if opts.get("sensitivity") is not None:
        cfg["endpoint_sensitivity"] = opts["sensitivity"]
    if opts.get("level") is not None:
        cfg["endpoint_latency_adjustment_level"] = opts["level"]
    if opts.get("context"):
        cfg["context"] = opts["context"]

    res = {"ended": False, "wait": None, "first_token": None,
           "text": "", "nonfinal_seen": 0, "error": None,
           "last_tok_ms": 0, "end_proc_ms": None, "wall_wait": None}
    finals, nonfinals = [], []
    try:
        async with websockets.connect(URL, max_size=None,
                                      open_timeout=20, close_timeout=5) as ws:
            await ws.send(json.dumps(cfg))
            t0 = time.monotonic()
            done = asyncio.Event()

            async def send():
                step = int(rate * CHUNK_MS / 1000) * 2
                for i in range(0, len(pcm), step):
                    if done.is_set():
                        return
                    await ws.send(pcm[i:i + step])
                    await asyncio.sleep(CHUNK_MS / 1000)
                try:
                    await ws.send(b"")
                except Exception:
                    pass

            task = asyncio.create_task(send())
            budget = len(pcm) / (2 * rate) + 12
            try:
                while True:
                    raw = await asyncio.wait_for(ws.recv(), timeout=budget)
                    at = time.monotonic() - t0
                    msg = json.loads(raw) if isinstance(raw, str) else {}
                    if msg.get("error_code") or msg.get("error_message"):
                        res["error"] = str(msg)[:200]
                        break
                    for tok in msg.get("tokens", []) or []:
                        txt = tok.get("text", "")
                        if txt in END_TOKENS:
                            if tok.get("is_final"):
                                # Measured in the AUDIO timeline, not wall
                                # clock. Pacing 100ms chunks with asyncio.sleep
                                # drifts ~10% on Windows, which inflated the
                                # first run of this probe by ~0.4s and would
                                # have condemned a config that was fine.
                                # Soniox's own `final_audio_proc_ms` says where
                                # in the audio the decision landed; the last
                                # real token's `end_ms` says where speech
                                # actually stopped. The gap between them is the
                                # wait a caller sits through.
                                res["ended"] = True
                                res["end_proc_ms"] = msg.get(
                                    "final_audio_proc_ms")
                                res["wall_wait"] = max(0.0, at - speech_s)
                                done.set()
                            continue
                        if tok.get("is_final"):
                            finals.append(txt)
                        else:
                            nonfinals.append(txt)
                            res["nonfinal_seen"] += 1
                        if tok.get("end_ms"):
                            res["last_tok_ms"] = max(res["last_tok_ms"],
                                                     tok["end_ms"])
                        if res["first_token"] is None:
                            res["first_token"] = at
                    if res["ended"] or msg.get("finished"):
                        break
            except (asyncio.TimeoutError, websockets.ConnectionClosed):
                pass
            finally:
                task.cancel()
    except Exception as e:
        res["error"] = (type(e).__name__ + ": " + str(e))[:200]
    res["text"] = "".join(finals).strip() or "".join(nonfinals).strip()
    if res["ended"] and res["end_proc_ms"] and res["last_tok_ms"]:
        res["wait"] = max(0.0, (res["end_proc_ms"] - res["last_tok_ms"]) / 1000.0)
    elif res["ended"]:
        # No usable audio timestamps; fall back to wall clock and say so.
        res["wait"] = res["wall_wait"]
    return res


async def run_set(rows, key, opts, concurrency, verbose):
    cache = {}
    sem = asyncio.Semaphore(concurrency)
    out = []
    total = len(rows)

    async def work(r):
        p = find_audio(r["workflow"], r["run"])
        if p is None:
            return
        if p not in cache:
            cache[p] = load_call(p)
        pcm, rate = cache[p]
        clip, speech_s = slice_burst(pcm, rate, r["start"], r["end"],
                                     r.get("gap_after"))
        if len(clip) < rate:          # under 0.5s of audio, not a fair test
            return
        async with sem:
            res = await one_burst(key, clip, rate, speech_s, opts)
        res.update(run=r["run"], label=r["was_turn_end"],
                   gap=r.get("gap_after"), ref=r.get("text", ""),
                   speech=speech_s)
        out.append(res)
        if verbose:
            mark = "END " if res["ended"] else "hold"
            w = ("%.2fs" % res["wait"]) if res["wait"] is not None else "  -  "
            ok = "ok " if res["ended"] == res["label"] else "MISS"
            lab = "end " if r["was_turn_end"] else "mid "
            body = (res["text"] or res["error"] or "")[:40]
            print("  [%3d/%3d] run%-5s label=%s %s %s %s  %s"
                  % (len(out), total, r["run"], lab, mark, w, ok, body))

    rows = sorted(rows, key=lambda r: (r["run"], r["start"]))
    await asyncio.gather(*(work(r) for r in rows))
    return out


def report(res, opts):
    ok = [r for r in res if r["error"] is None]
    err = [r for r in res if r["error"] is not None]
    ends = [r for r in ok if r["label"]]
    mids = [r for r in ok if not r["label"]]

    print("\n" + "=" * 66)
    print("SONIOX  model=%s  max_delay=%sms  sens=%s  level=%s"
          % (opts["model"], opts["max_delay"], opts.get("sensitivity"),
             opts.get("level")))
    print("=" * 66)
    print("  bursts %d ok, %d errored   (%d real ends / %d mid-turn)"
          % (len(ok), len(err), len(ends), len(mids)))
    if err:
        print("  first error: " + str(err[0]["error"]))
    if not ok:
        return

    cut = [r for r in mids if r["ended"]]
    cut_rate = len(cut) / len(mids) if mids else 0.0
    hit = [r for r in ends if r["ended"]]
    waits = [r["wait"] for r in hit if r["wait"] is not None]

    print("\n  FALSE CUTOFFS  %d/%d = %.1f%%      bar is 2.0%%   "
          "(live detector 6.45%%, dumb 0.6s timer 19.1%%)"
          % (len(cut), len(mids), cut_rate * 100))
    if waits:
        sw = sorted(waits)
        p90 = sw[max(0, int(len(sw) * 0.9) - 1)]
        print("  WAIT on real ends  p50 %.3fs  mean %.3fs  p90 %.3fs  max %.3fs"
              % (st.median(waits), st.mean(waits), p90, max(waits)))
        print("  ends detected  %d/%d = %.0f%%"
              % (len(hit), len(ends), len(hit) / len(ends) * 100))
        under = sum(1 for w in waits if w < 0.6)
        print("  under 0.6s  %d/%d      <- production has managed 0 of 69"
              % (under, len(waits)))
    else:
        print("  WAIT: no real end was ever detected. This is a hold-forever "
              "config, not a detector.")

    part = [r for r in ok if r["nonfinal_seen"] > 0]
    print("\n  streamed non-final tokens on %d/%d bursts      "
          "<- saarika:v2.5 emits none at all" % (len(part), len(ok)))
    firsts = [r["first_token"] for r in ok
              if r["first_token"] is not None and r["speech"] > 0
              and r["first_token"] < r["speech"]]
    if firsts:
        print("  first token arrived DURING speech on %d bursts, p50 %.2fs in"
              % (len(firsts), st.median(firsts)))

    got = [r for r in ok if r["text"]]
    print("  produced text on %d/%d bursts" % (len(got), len(ok)))
    pairs = [x for x in ok if x["text"] and x["ref"]][:6]
    if pairs:
        print("\n  --- sample, Soniox vs the corpus reference (Sarvam) ---")
        for r in pairs:
            print("    soniox: " + r["text"][:64])
            print("    sarvam: " + r["ref"][:64])
    print("")

    if mids and waits:
        print("  VERDICT")
        med = st.median(waits)
        if cut_rate > 0.0645:
            print("    false cutoffs %.1f%% are WORSE than the live detector's "
                  "6.45%%. Cardinal failure -- do not ship this config."
                  % (cut_rate * 100))
        elif med >= 0.9:
            print("    wait p50 %.3fs does not beat the 0.9s blind floor. "
                  "No latency win here." % med)
        else:
            print("    cutoffs %.1f%% (bar 2.0%%, live 6.45%%) and wait p50 "
                  "%.3fs against a 0.9s floor." % (cut_rate * 100, med))
            print("    Endpoint leg would move 0.906s -> %.3fs, "
                  "turn 1.555s -> %.3fs." % (med, 1.555 - 0.906 + med))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workflow", default="2", help="default 2 = MB Solar")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--model", default="stt-rt-v5")
    ap.add_argument("--max-delay", type=int, default=2000,
                    help="Soniox default is 2000ms, which alone exceeds the "
                         "whole turn budget. Set it.")
    ap.add_argument("--sensitivity", type=float, default=None)
    ap.add_argument("--level", type=int, default=None)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    if not CORPUS.exists():
        raise SystemExit("missing " + str(CORPUS))
    rows = [json.loads(l) for l in CORPUS.open(encoding="utf-8")]
    rows = [r for r in rows if r["workflow"] == a.workflow
            and find_audio(r["workflow"], r["run"])]
    # A burst the reference transcriber found no words in cannot produce a
    # SEMANTIC endpoint, so scoring it measures nothing about the model. 80 of
    # workflow 2's 194 bursts are empty; including them was diluting the first
    # run's "ends detected" figure with cases no detector could ever hit.
    blank = [r for r in rows if not (r.get("text") or "").strip()]
    rows = [r for r in rows if (r.get("text") or "").strip()]
    print("dropped %d wordless bursts; %d carry a transcript" % (len(blank), len(rows)))
    if a.limit:
        # Keep the label balance when truncating, or the cutoff rate is noise.
        t = [r for r in rows if r["was_turn_end"]][:a.limit // 2]
        f = [r for r in rows if not r["was_turn_end"]][:a.limit - len(t)]
        rows = t + f
    print("workflow %s: %d bursts (%d ends / %d mid-turn)"
          % (a.workflow, len(rows),
             sum(1 for r in rows if r["was_turn_end"]),
             sum(1 for r in rows if not r["was_turn_end"])))

    key = env("SONIOX_API_KEY")
    configs = [{"model": a.model, "max_delay": a.max_delay,
                "sensitivity": a.sensitivity, "level": a.level}]
    if a.sweep:
        configs = [
            {"model": a.model, "max_delay": 2000, "sensitivity": None, "level": None},
            {"model": a.model, "max_delay": 1000, "sensitivity": 0.0, "level": 1},
            {"model": a.model, "max_delay": 800, "sensitivity": 0.3, "level": 2},
            {"model": a.model, "max_delay": 600, "sensitivity": 0.5, "level": 3},
            {"model": a.model, "max_delay": 500, "sensitivity": -0.3, "level": 2},
        ]
    for cfg in configs:
        res = asyncio.run(run_set(rows, key, cfg, a.concurrency, not a.quiet))
        report(res, cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
