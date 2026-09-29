#!/usr/bin/env python
"""One bench for every end-of-turn policy, on production's own VAD, clock-corrected.

Why a second harness
--------------------
`replay_turns.replay()` drives ONE analyzer. Production does not run one
analyzer: it runs pipecat's 0.7 s speech timer RACING the Telugu assist, and
whichever fires first ends the turn (`turn_taking.py`, `telugu_turn_assist`).
So the number for the arrangement callers actually hear had never been measured.

Two instrument defects were found building this, and both are fixed here:

1. The energy VAD is not production's VAD. `true_latency.regions` thresholds at
   0.35 x the 75th-percentile frame energy of the caller track. A caller track is
   mostly line noise, so that threshold sits inside the noise: on run 1021 it
   found a 5.1 s "speech burst" around a one-second "ఆ... మాట్లాడొచ్చు", and 30
   regions where Silero finds 19, each of which lines up with a transcript.
   Production's turn strategies are driven by Silero (`VADParams(stop_secs=0.2)`,
   confidence 0.7, start 0.2, min_volume 0.6), so the latch here IS Silero, run
   offline through pipecat's own `SileroVADAnalyzer`.

2. The recording clock drifts against the event log by 0.8-1.4% (measured: run
   1021 slope 1.0082, run 992 slope 1.0141). Two minutes into a call the logged
   transcript "arrives" half a second BEFORE the caller stops talking, which would
   flatter any policy that reads the words. Each call gets a linear map
   event-time -> audio-time, fitted on transcript starts against Silero onsets
   (the payload start IS the VAD-start event, so the pairs are like for like).

Transcript timing, two modes:

    logged   the final arrives when that call's log says it did (mapped). Honest
             about whatever STT the call ran: Sarvam 0.39 s, Soniox global 0.69 s,
             Soniox India 0.25 s.
    stack    the final arrives STT_LATENCY after Silero's stop, i.e. what today's
             stack (Soniox India, finalize-on-VAD-stop) would deliver. This is the
             forward-looking number.

Cut-off rule unchanged from replay_turns / telugu_turn: the caller's VAD starts
again within RESUME_WINDOW_S (1.0 s) of the turn being ended.

    python tools/turnsense/replay_eval.py --policy live --policy timer:0.70
    python tools/turnsense/replay_eval.py --stopwatch
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import wave
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
from pathlib import Path

import numpy as np

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

REPO = Path(__file__).resolve().parents[2]
VAANI = REPO.parent / "dograh-vapi"
sys.path.insert(0, str(VAANI))
sys.path.insert(0, str(REPO / "tools"))
sys.path.insert(0, str(REPO / "tools" / "turnsense"))

FRESH = REPO / ".tmp" / "audio_new" / "caller"
RUNS = REPO / ".tmp" / "harvest" / "runs"
PREP = REPO / ".tmp" / "turnsense" / "prep_silero"

FRAME_MS = 20
VAD_STOP_S = 0.2           # production VADParams(stop_secs=0.2)
RESUME_WINDOW_S = 1.0      # telugu_turn.RESUME_WINDOW_S
# Soniox India, finalisation after VAD stop, measured live 23 Sep (doc 40):
# 0.23-0.28 s across ~40 turns. The middle of that band.
STT_LATENCY = 0.25

# wf2's stored turn config as of 23 Sep (.tmp/wf2_config.json) -- the values the
# live assist analyzer is built with. The gate of 24 Sep pins the same switches.
LIVE_WF2 = {
    "endpoint_min_secs": 0.3, "endpoint_max_secs": 2.2,
    "endpoint_unsure_floor_secs": 0.9, "endpoint_unsure_band": 0.9,
    "turn_model": "linear", "smart_turn_stop_secs": 0.2,
    "dograh_speech_timeout_secs": 0.7, "telugu_turn_assist": True,
}


def _quiet_logs():
    from loguru import logger
    logger.remove()


# --------------------------------------------------------------------------
# The corpus
# --------------------------------------------------------------------------

def _ts(v) -> float:
    return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()


def calls(dir_: Path = FRESH, limit: int | None = None,
          need_rt: bool = False) -> list[tuple[str, Path, Path]]:
    out = []
    for wav in sorted(dir_.glob("*.wav")):
        try:
            wf, rid = wav.stem.split("_run")
        except ValueError:
            continue
        log = RUNS / f"{wf[2:]}_{rid}.json"
        if need_rt and not (REPO / ".tmp" / "turnsense" / "rt" / f"{wav.stem}.json").exists():
            continue
        if log.exists():
            out.append((wav.stem, wav, log))
    return out[:limit] if limit else out


async def _silero_latch(pcm: bytes, sr: int) -> list[int]:
    from pipecat.audio.vad.silero import SileroVADAnalyzer
    from pipecat.audio.vad.vad_analyzer import VADParams, VADState
    v = SileroVADAnalyzer(sample_rate=sr, params=VADParams(stop_secs=VAD_STOP_S))
    v.set_sample_rate(sr)
    step = int(sr * FRAME_MS / 1000) * 2
    out = []
    for i in range(0, len(pcm) - step, step):
        s = await v.analyze_audio(pcm[i:i + step])
        out.append(1 if s in (VADState.SPEAKING, VADState.STOPPING) else 0)
    return out


def segments(latch: list[int]) -> list[tuple[float, float]]:
    """(onset, quiet) in seconds: when the VAD said started / said stopped."""
    out, start = [], None
    for i, v in enumerate(latch):
        t = i * FRAME_MS / 1000.0
        if v and start is None:
            start = t
        elif not v and start is not None:
            out.append((start, t))
            start = None
    if start is not None:
        out.append((start, len(latch) * FRAME_MS / 1000.0))
    return out


def _events(run: dict):
    ev = (run.get("logs") or {}).get("realtime_feedback_events") or []
    if not ev:
        return [], [], None
    base = _ts(ev[0]["timestamp"])
    finals, agent = [], []
    for e in ev:
        p = e.get("payload") or {}
        if e.get("type") == "rtf-user-transcription" and p.get("final"):
            start = _ts(p.get("timestamp") or e["timestamp"]) - base
            arrive = _ts(e["timestamp"]) - base
            finals.append({"start": start, "arrive": arrive,
                           "text": (p.get("text") or "").strip()})
        elif e.get("type") == "rtf-bot-text" and p.get("text"):
            agent.append({"start": _ts(p.get("timestamp") or e["timestamp"]) - base,
                          "text": str(p["text"]).strip()})
    return finals, sorted(agent, key=lambda a: a["start"]), base


def fit_clock(finals, segs) -> tuple[float, float, int, float]:
    """audio_t = a + b * event_t, from transcript starts vs Silero onsets."""
    on = np.array([s for s, _ in segs])
    st = np.array([f["start"] for f in finals])
    if len(on) < 3 or len(st) < 3:
        return 0.0, 1.0, 0, float("nan")
    a, b = 0.0, 1.0
    pairs = np.empty((0, 2))
    for tol in (2.0, 1.0, 0.6):
        pred = a + b * st
        found = []
        for s, p in zip(st, pred):
            j = int(np.argmin(np.abs(on - p)))
            if abs(on[j] - p) < tol:
                found.append((s, on[j]))
        pairs = np.array(found)
        if len(pairs) < 3:
            return 0.0, 1.0, len(pairs), float("nan")
        if len(pairs) >= 4 and np.ptp(pairs[:, 0]) > 20:
            b, a = np.polyfit(pairs[:, 0], pairs[:, 1], 1)
            b = float(min(1.03, max(0.97, b)))
            a = float(np.median(pairs[:, 1] - b * pairs[:, 0]))
        else:
            a, b = float(np.median(pairs[:, 1] - pairs[:, 0])), 1.0
    resid = pairs[:, 1] - (a + b * pairs[:, 0])
    return a, b, len(pairs), float(np.std(resid))


def stack_arrivals(finals_audio, segs) -> list[tuple[float, str]]:
    """When today's stack would deliver each final: Silero stop + STT_LATENCY.

    Soniox finalises on every VAD stop, so a final the log shows spanning two
    VAD segments ("సంవత్సరాలు, మరి.\\nసొంత ఇల్లు.") would have arrived in two
    pieces. Lines are handed to the covered segments in order.
    """
    out = []
    for f in finals_audio:
        cov = [(o, q) for o, q in segs
               if q > f["start"] - 0.4 and o < f["arrive"] + 0.2]
        lines = [x for x in f["text"].split("\n") if x.strip()] or [f["text"]]
        if not cov:
            out.append((f["arrive"], f["text"]))       # VAD never heard it
            continue
        if len(lines) >= len(cov):
            chunks = [[x] for x in lines[:len(cov) - 1]] + [lines[len(cov) - 1:]]
            for (o, q), ch in zip(cov, chunks):
                out.append((q + STT_LATENCY, " ".join(ch)))
        else:
            # fewer lines than segments: the text lands after the last one
            out.append((cov[-1][1] + STT_LATENCY, " ".join(lines)))
    return sorted(out)


def prepare(job) -> dict:
    """Everything a replay needs from one call, cached: Silero is the slow part."""
    stem, wav, log = job
    PREP.mkdir(parents=True, exist_ok=True)
    cache = PREP / f"{stem}.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    _quiet_logs()
    with wave.open(str(wav)) as w:
        sr = w.getframerate()
        pcm = w.readframes(w.getnframes())
    latch = asyncio.run(_silero_latch(pcm, sr))
    segs = segments(latch)
    run = json.loads(Path(log).read_text(encoding="utf-8"))
    finals, agent, _ = _events(run)
    a, b, n, sd = fit_clock(finals, segs)

    def m(t):
        return a + b * t

    fa = [{"start": m(f["start"]), "arrive": m(f["arrive"]), "text": f["text"]}
          for f in finals]
    d = {
        "stem": stem, "sr": sr, "latch": latch, "segments": segs,
        "clock": {"a": a, "b": b, "pairs": n, "resid_sd": sd},
        "finals_logged": [(f["arrive"], f["text"]) for f in fa],
        "finals_stack": stack_arrivals(fa, segs),
        "finals_starts": [f["start"] for f in fa],
        "agent": [(m(x["start"]), x["text"]) for x in agent],
        "workflow": int(stem.split("_run")[0][2:]),
    }
    cache.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    return d


# --------------------------------------------------------------------------
# Policies
# --------------------------------------------------------------------------

class Stopwatch:
    """Pipecat's SpeechTimeoutUserTurnStopStrategy, frame-exact.

    Fires `wait` seconds after the VAD says stopped; any speech cancels it. With
    Silero's 0.2 s stop that is 0.2 + wait after the last word -- the flat 0.90 s
    every wf2 call of 23 Sep logged as `endpoint_secs`.
    """

    def __init__(self, wait: float, sr: int):
        self.wait, self.sr = wait, sr
        self.silence, self.armed = 0.0, False

    def append_audio(self, buf: bytes, is_speech: bool):
        from pipecat.audio.turn.base_turn_analyzer import EndOfTurnState
        if is_speech:
            self.silence, self.armed = 0.0, True
            return EndOfTurnState.INCOMPLETE
        if not self.armed:
            return EndOfTurnState.INCOMPLETE
        self.silence += (len(buf) // 2) / self.sr
        if self.silence >= self.wait - 1e-9:
            self.armed = False
            return EndOfTurnState.COMPLETE
        return EndOfTurnState.INCOMPLETE

    def clear(self):
        self.silence, self.armed = 0.0, False

    def note_text(self, text: str):
        pass


class Race:
    """Several strategies; the first to fire ends the turn; all reset together."""

    def __init__(self, *members):
        self.members = members

    def append_audio(self, buf: bytes, is_speech: bool):
        from pipecat.audio.turn.base_turn_analyzer import EndOfTurnState
        fired = False
        for m in self.members:
            if m.append_audio(buf, is_speech) == EndOfTurnState.COMPLETE:
                fired = True
        if fired:
            for m in self.members:
                if hasattr(m, "clear"):
                    m.clear()
            return EndOfTurnState.COMPLETE
        return EndOfTurnState.INCOMPLETE

    def note_text(self, text: str):
        for m in self.members:
            if hasattr(m, "note_text"):
                m.note_text(text)

    def note_agent(self, text: str):
        for m in self.members:
            if hasattr(m, "note_agent"):
                m.note_agent(text)


def telugu_analyzer(sr: int, cfg: dict = LIVE_WF2, **over):
    """The live assist analyzer, built from config the way turn_taking builds it."""
    import api.services.vaani.telugu_turn as tt
    p = tt.TeluguTurnParams(
        stop_secs=float(cfg.get("smart_turn_stop_secs", 0.2)),
        min_endpoint_secs=float(cfg["endpoint_min_secs"]),
        max_endpoint_secs=float(cfg["endpoint_max_secs"]),
        unsure_floor_secs=float(cfg["endpoint_unsure_floor_secs"]),
        unsure_band=float(cfg["endpoint_unsure_band"]),
        **over,
    )
    a = tt.TeluguTurnAnalyzer(sample_rate=sr, params=p,
                              model_kind=str(cfg.get("turn_model", "forest")))
    a.set_sample_rate(sr)
    return a


def build(policy: str, sr: int):
    """Policy name -> object. Kept as strings so workers can rebuild them."""
    if policy.startswith("timer:"):
        return Stopwatch(float(policy.split(":", 1)[1]), sr)
    if policy == "live":
        return Race(Stopwatch(LIVE_WF2["dograh_speech_timeout_secs"], sr),
                    telugu_analyzer(sr))
    if policy == "assist-alone":
        return telugu_analyzer(sr)
    if policy == "live-eager":
        # Run 997 (20 Sep) ended 19 of 20 turns 0.30-0.38 s after the last word:
        # 0.2 s of Silero + ~0.12 s -- the analyzer's MIN_SILENCE_MS -- which is
        # before any transcript exists. That is only possible with the blind
        # floors at zero, so this is the assist as it actually behaved that day.
        return Race(Stopwatch(LIVE_WF2["dograh_speech_timeout_secs"], sr),
                    telugu_analyzer(sr, blind_min_silence_ms=0.0,
                                    blind_short_silence_ms=0.0))
    if policy == "analyzer-doc41":
        import api.services.vaani.telugu_turn as tt
        a = tt.TeluguTurnAnalyzer(sample_rate=sr, params=tt.TeluguTurnParams())
        a.set_sample_rate(sr)
        return a
    if policy.startswith("turnsense"):
        import turnsense_policy as TP
        return TP.from_spec(policy, sr)
    raise SystemExit(f"unknown policy {policy!r}")


# --------------------------------------------------------------------------
# Replay + score
# --------------------------------------------------------------------------

class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def replay_one(args) -> dict:
    stem, wav, log, policy, mode = args
    _quiet_logs()
    import api.services.vaani.telugu_turn as tt
    from pipecat.audio.turn.base_turn_analyzer import EndOfTurnState

    d = prepare((stem, wav, log))
    sr = d["sr"]
    with wave.open(str(wav)) as w:
        pcm = w.readframes(w.getnframes())
    latch = d["latch"]
    if mode == "rt":
        # Soniox stt-rt-v5 text for EVERY pause, as production would have had
        # it (soniox_rt_segments.py), landing STT_LATENCY after Silero's stop.
        rtp = REPO / ".tmp" / "turnsense" / "rt" / f"{stem}.json"
        rt = json.loads(rtp.read_text(encoding="utf-8")) if rtp.exists() else {}
        finals = sorted((sg["quiet"] + STT_LATENCY, sg["text"])
                        for sg in rt.get("segments", []) if sg.get("text"))
    else:
        finals = d["finals_stack"] if mode == "stack" else d["finals_logged"]
    agent = d["agent"]
    segs = [tuple(s) for s in d["segments"]]

    clock = _Clock()
    real = tt.time.monotonic
    tt.time.monotonic = clock
    try:
        if policy.startswith("oracle"):
            # The ceiling for ANY policy that decides when the words land: it
            # knows whether he will speak again within 1.5 s of his last word.
            # Not buildable; it says how much of the gap a perfect reader of the
            # words could close under this exact instrument.
            import turnsense_policy as TP
            onsets = [o for o, _ in segs]
            quiets = [q for _, q in segs]

            class _Oracle:
                def __init__(self, sr):
                    self.p = TP.from_spec("turnsense" + policy[6:], sr)

                def _truth(self):
                    now = clock.t
                    q = max((x for x in quiets if x <= now + 1e-6), default=None)
                    if q is None:
                        return 0.0
                    nxt = [o for o in onsets if o > q]
                    return 0.0 if nxt and nxt[0] - (q - VAD_STOP_S) <= 1.5 else 1.0

                def append_audio(self, buf, is_speech):
                    return self.p.append_audio(buf, is_speech)

                def note_text(self, text):
                    self.p.note_text(text)
                    if self.p._fresh:
                        self.p._p = self._truth()

                def note_agent(self, text):
                    self.p.note_agent(text)

            p = _Oracle(sr)
        else:
            p = build(policy, sr)
        step = int(sr * FRAME_MS / 1000) * 2
        completes, resumes = [], []
        ended_at, was = None, False
        ni = ai = 0
        for i in range(0, len(pcm) - step, step):
            fi = i // step
            t = fi * FRAME_MS / 1000.0
            clock.t = t
            while ai < len(agent) and agent[ai][0] <= t:
                if hasattr(p, "note_agent"):
                    p.note_agent(agent[ai][1])
                ai += 1
            while ni < len(finals) and finals[ni][0] <= t:
                p.note_text(finals[ni][1])
                ni += 1
            speaking = bool(latch[fi]) if fi < len(latch) else False
            if p.append_audio(pcm[i:i + step], speaking) == EndOfTurnState.COMPLETE:
                completes.append(t)
                ended_at = t
            if (ended_at is not None and speaking and not was
                    and t - ended_at < RESUME_WINDOW_S):
                resumes.append(t)
                ended_at = None
            was = speaking
    finally:
        tt.time.monotonic = real

    # Waits are measured from the LAST WORD: Silero's stop minus its 0.2 s hold.
    # `good_waits` are the waits on verdicts the caller did NOT contradict --
    # the latency a caller actually experiences when the turn really was over.
    waits, good, cut_at = [], [], []
    onsets = [o for o, _ in segs]
    for t in completes:
        prior = [q for _, q in segs if q <= t + 1e-6]
        if not prior:
            continue
        w = t - (max(prior) - VAD_STOP_S)
        waits.append(w)
        nxt = [o for o in onsets if o > t]
        if nxt and min(nxt) - t < RESUME_WINDOW_S:
            cut_at.append(t)
        else:
            good.append(w)
    return {"stem": stem, "bursts": len(segs), "verdicts": len(completes),
            "cutoffs": len(resumes), "waits": waits, "good_waits": good,
            "cut_at": cut_at}


def evaluate(policy: str, jobs, workers: int = 10, mode: str = "stack") -> dict:
    args = [(s, str(w), str(l), policy, mode) for s, w, l in jobs]
    with ProcessPoolExecutor(max_workers=workers) as ex:
        rows = list(ex.map(replay_one, args, chunksize=2))
    bursts = sum(r["bursts"] for r in rows)
    cut = sum(r["cutoffs"] for r in rows)
    q = sorted(w for r in rows for w in r["good_waits"])
    return {
        "policy": policy, "mode": mode, "calls": len(rows), "bursts": bursts,
        "verdicts": sum(r["verdicts"] for r in rows),
        "cutoffs": cut, "cut_pct": 100.0 * cut / max(1, bursts),
        "wait_p50": statistics.median(q) if q else None,
        "wait_p90": q[int(len(q) * 0.9)] if len(q) > 9 else None,
        "slow": sum(1 for w in q if w > 1.5),
        "rows": rows,
    }


HEADER = (f"{'policy':40} {'bursts':>6} {'cut':>5} {'cut%':>7} "
          f"{'wait50':>7} {'wait90':>7} {'slow':>5}")


def line(res: dict) -> str:
    def f(v):
        return f"{v:.2f}" if v is not None else "-"
    return (f"{res['policy']:40} {res['bursts']:>6} {res['cutoffs']:>5} "
            f"{res['cut_pct']:>6.1f}% {f(res['wait_p50']):>7} "
            f"{f(res['wait_p90']):>7} {res['slow']:>5}")


def prepare_all(jobs, workers: int):
    todo = [j for j in jobs if not (PREP / f"{j[0]}.json").exists()]
    if todo:
        print(f"running Silero over {len(todo)} calls ...", flush=True)
        with ProcessPoolExecutor(max_workers=workers) as ex:
            list(ex.map(prepare, [(s, str(w), str(l)) for s, w, l in todo],
                        chunksize=1))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--policy", action="append", default=[])
    ap.add_argument("--stopwatch", action="store_true",
                    help="the model-free control curve")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--dir", default=str(FRESH))
    ap.add_argument("--mode", choices=("rt", "stack", "logged"), default="rt")
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--save", help="write full results JSON here")
    ap.add_argument("--only-calls", help="file with one call stem per line")
    a = ap.parse_args()

    jobs = calls(Path(a.dir), a.limit, need_rt=(a.mode == "rt"))
    if a.only_calls:
        keep = set(Path(a.only_calls).read_text(encoding="utf-8").split())
        jobs = [j for j in jobs if j[0] in keep]
    prepare_all(jobs, a.workers)
    pols = list(a.policy)
    if a.stopwatch:
        pols += [f"timer:{w:.2f}" for w in
                 (0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 1.00, 1.20, 1.50, 2.00)]
    print(f"\n{len(jobs)} calls, transcript timing = {a.mode}\n")
    print(HEADER)
    print("-" * len(HEADER))
    out = []
    for p in pols:
        res = evaluate(p, jobs, a.workers, a.mode)
        out.append(res)
        print(line(res), flush=True)
    if a.save:
        Path(a.save).parent.mkdir(parents=True, exist_ok=True)
        Path(a.save).write_text(json.dumps(out, ensure_ascii=False, indent=1),
                                encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
