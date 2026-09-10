#!/usr/bin/env python
"""Replay a real recorded call through the turn detector and count interruptions.

Why this exists
---------------
Every turn-detection change so far was verified by placing a live call to the
client's own customers. Two of them were wrong, and the client's callers found
both -- run 790 cut a man off twice and hung up on a 2,25,000/month lead; run
792 was worse. There was no way to test the detector without spending a real
call, so every fix was a guess.

This spends no calls. 651 caller-only recordings and 2,097 run logs are already
on disk from earlier harvests.

What it measures, and why it is TWO numbers
-------------------------------------------
The detector fails in opposite directions and a single score hides that:

    CUT OFF   it said COMPLETE while the caller was still inside a speech burst
    WAITED    it said COMPLETE long after the burst ended -- the caller sits in
              silence wondering whether the agent heard them

Measured on run 790 from the event stream, the caller waited 3.78s, 5.34s and
5.29s on three turns of the SAME call in which two other turns were cut off at
0.02s. Tuning one threshold cannot fix a decision that is wrong in both
directions, which is why the 0.35s timer and `endpoint_min_secs` both failed.

How the replay stays faithful
-----------------------------
`append_audio` accumulates silence from the SAMPLE COUNT, not the clock, so a
replay is exact at any speed. Three things still have to be got right:

  * `is_speech` is the VAD's LATCHED user-speaking flag, not per-frame energy.
    It stays True through pauses inside one sentence and drops only after
    `VADParams.stop_secs` (0.2s in production) of quiet. Built here by energy-
    VADing the caller track with `true_latency.regions` -- the same function
    that measures真 latency -- then applying that same 0.2s release.
  * `note_text` must be fed, or the text half of the decision is missing and the
    replay flatters the model. `completeness.sounds_unfinished` can only ever
    ADD holding, so without it every result is optimistic.
  * `_ended_at` / `_cutoffs` are the analyzer's ONLY wall-clock dependency.
    Left alone in a faster-than-realtime replay every resume looks instant,
    `_cutoffs` runs past `CUTOFFS_BEFORE_ADAPTING`, `_band()` pins to 1.0, and
    the replay is quietly more patient than production. Time is faked instead.

    python tools/replay_turns.py --run 790
    python tools/replay_turns.py --all --limit 40
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import statistics
import sys
import urllib.request
import wave
from datetime import datetime
from pathlib import Path

import numpy as np

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

REPO = Path(__file__).resolve().parents[1]
DOGRAH = REPO.parent / "dograh-vapi"
sys.path.insert(0, str(DOGRAH))

CALLER_DIR = REPO / ".tmp" / "audio" / "caller"
RUNS_DIR = REPO / ".tmp" / "harvest" / "runs"

# Production VAD release. `SileroVADAnalyzer(params=VADParams(stop_secs=0.2))`
# in run_pipeline.py -- the latch this replay has to reproduce.
VAD_STOP_S = 0.2
FRAME_MS = 20          # what the transport feeds live; 320 bytes at 8 kHz


def _load_tool(name: str):
    spec = importlib.util.spec_from_file_location(name, REPO / "tools" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    argv, sys.argv = sys.argv, [name]
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.argv = argv
    return mod


class _Clock:
    """A fake `time.monotonic`, advanced by the audio actually consumed.

    The analyzer's cut-off self-detection compares `time.monotonic()` against
    `_ended_at`, and its buffer trim does the same. Under a replay that runs
    faster than realtime those comparisons are meaningless -- every resume
    lands inside `RESUME_WINDOW_S`, so `_cutoffs` climbs, `_band()` pins to 1.0
    and the model becomes more patient than it would ever be on a call. Driving
    the clock from consumed samples makes the replay behave exactly as
    production would.
    """

    def __init__(self):
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def speech_latch(x: np.ndarray, sr: int, regions_fn) -> tuple[list, list]:
    """(speech regions, per-frame latched is_speech) for the caller track."""
    regions = regions_fn(x, sr)
    n_frames = len(x) // int(sr * FRAME_MS / 1000)
    latch = [False] * n_frames
    hold = int(VAD_STOP_S * 1000 / FRAME_MS)
    for start, end in regions:
        a = int(start * 1000 / FRAME_MS)
        b = int(end * 1000 / FRAME_MS) + hold      # the VAD's release
        for i in range(max(0, a), min(n_frames, b)):
            latch[i] = True
    return regions, latch


def transcripts(run: dict) -> list[tuple[float, str]]:
    """(seconds from first event, text) for each final caller transcript."""
    events = (run.get("logs") or {}).get("realtime_feedback_events") or []
    if not events:
        return []

    def ts(v):
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()

    base = ts(events[0]["timestamp"])
    out = []
    for e in events:
        p = e.get("payload") or {}
        if e.get("type") == "rtf-user-transcription" and p.get("final"):
            out.append((ts(e["timestamp"]) - base, (p.get("text") or "").strip()))
    return out


# Which model artifact the analyzer loads. `TeluguTurnAnalyzer` prefers the
# forest and falls back to the regression, so selecting the LINEAR candidate
# means pointing the forest at a path that does not exist -- which the loader
# already treats as "no forest, use the regression" rather than as an error.
#
# Without this the replay could only ever score the artifact that happens to sit
# in the deployed path, so "is the candidate better ON A CALL" could not be
# asked. The isolated classifier numbers do not answer it: `_wait_secs`
# interpolates on `frac = p / bar`, so a new model changes the WAIT as well as
# the verdict, and the two can move in opposite directions.
_NONE = REPO / "models" / "__no_such_forest__.json"
MODELS = {
    "shipped":     (None, None),                      # whatever is deployed
    "real-gbm":    (REPO / "models" / "audio_turn_real_gbm.json", None),
    "real-linear": (_NONE, REPO / "models" / "audio_turn_real.json"),
}


def _build(detector: str, sr: int, params, tt, model: str = "shipped"):
    """The analyzer under test.

    The client's question, 6 Sep: "pipecat already has a turn detector, why are
    you training one". Fair, and it is answered here with a number rather than
    an opinion. `smart-turn-v3.2` ships bundled with pipecat, runs offline, and
    is a Whisper-mel -> single-logit binary classifier with no language metadata
    -- so whether it transfers to Telugu is an empirical question, not a
    documented one. Neither Deepgram Flux (10 languages) nor LiveKit's
    multilingual detector (14) lists Telugu at all.

    Both analyzers implement `append_audio(buffer, is_speech) -> EndOfTurnState`,
    so the harness feeds them identically and scores them identically.
    """
    if detector == "audio-native":
        # The Telugu-retrained Whisper encoder. Same endpoint timers as
        # `telugu`, because it SUBCLASSES TeluguTurnAnalyzer -- only the
        # probability changes, so this measures the verdict and nothing else.
        from api.services.vaani.audio_native_turn import AudioNativeTurnAnalyzer
        return AudioNativeTurnAnalyzer(sample_rate=sr,
                                       params=params or tt.TeluguTurnParams())
    if detector == "smart-turn-v3":
        from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import (
            LocalSmartTurnAnalyzerV3)
        from pipecat.audio.turn.smart_turn.base_smart_turn import SmartTurnParams
        return LocalSmartTurnAnalyzerV3(
            sample_rate=sr,
            params=SmartTurnParams(stop_secs=tt.TeluguTurnParams().stop_secs))
    gbm, weights = MODELS.get(model, (None, None))
    return tt.TeluguTurnAnalyzer(sample_rate=sr,
                                 params=params or tt.TeluguTurnParams(),
                                 gbm_path=gbm, weights_path=weights)


def replay(wav: Path, run: dict, params=None, detector: str = "telugu",
           model: str = "shipped") -> dict:
    """Feed one recorded call through the shipped analyzer, frame by frame."""
    import api.services.vaani.telugu_turn as tt
    from pipecat.audio.turn.base_turn_analyzer import EndOfTurnState

    true_latency = _load_tool("true_latency")

    with wave.open(str(wav)) as w:
        sr = w.getframerate()
        pcm = w.readframes(w.getnframes())
    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)

    regions, latch = speech_latch(x, sr, true_latency.regions)
    said = transcripts(run)

    clock = _Clock()
    real_monotonic = tt.time.monotonic
    tt.time.monotonic = clock            # see _Clock
    try:
        a = _build(detector, sr, params, tt, model)
        # BaseTurnAnalyzer leaves `_sample_rate` at 0 until the pipeline calls
        # this; there is no pipeline here, and the vendor model divides by it.
        if hasattr(a, "set_sample_rate"):
            a.set_sample_rate(sr)
        if getattr(a, "enabled", True) is False:
            return {"error": "analyzer disabled -- model files missing"}

        step = int(sr * FRAME_MS / 1000) * 2      # bytes per 20 ms frame
        completes: list[float] = []
        # The analyzer's OWN cut-off detector, read out per frame.
        #
        # This is the authoritative signal, not my energy-VAD approximation:
        # `_cutoffs` is incremented by the same RESUME_WINDOW_S rule that runs
        # in production ("caller resumed 0.48s after we ended his turn"), so it
        # counts exactly what a live call would count. On run 790 it fires twice
        # -- matching what the client reported -- while burst-overlap scoring
        # saw nothing, because the energy VAD merges bursts the analyzer treats
        # as separate.
        resumes: list[float] = []
        seen_cutoffs = 0
        next_text = 0
        ended_at: float | None = None
        was_speaking = False
        for i in range(0, len(pcm) - step, step):
            frame_i = i // step
            t = frame_i * FRAME_MS / 1000.0
            clock.t = t
            # Transcripts arrive as the call progresses, exactly as live.
            while next_text < len(said) and said[next_text][0] <= t:
                # Audio-only detectors have no text half. That is a real
                # difference between the two, not a harness gap: our analyzer
                # reads the transcript because "అరవై" (sixty) and "సరే" (fine)
                # are the same shape of sound and only the words say one of them
                # is a quantity with the unit still to come.
                if hasattr(a, "note_text"):
                    a.note_text(said[next_text][1])
                next_text += 1
            speaking = latch[frame_i] if frame_i < len(latch) else False
            if a.append_audio(pcm[i:i + step], speaking) == EndOfTurnState.COMPLETE:
                completes.append(t)
                ended_at = t
                was_speaking = speaking
            # RESUME_WINDOW_S, applied from OUTSIDE the analyzer.
            #
            # TeluguTurnAnalyzer counts this itself in `_cutoffs`, and the
            # vendor model has no such instrumentation. Counting it here, by the
            # same rule, is what makes the two comparable: if the caller starts
            # again within a second of us ending his turn, we did not end it --
            # we interrupted it. Read off the same latch both detectors are fed.
            if (ended_at is not None and speaking and not was_speaking
                    and t - ended_at < tt.RESUME_WINDOW_S):
                seen_cutoffs += 1
                resumes.append(t)
                ended_at = None
            was_speaking = speaking
    finally:
        tt.time.monotonic = real_monotonic

    r = score(completes, regions)
    r["cutoffs"] = seen_cutoffs
    r["resume_at"] = resumes
    return r


def score(completes: list[float], regions: list) -> dict:
    """Classify every verdict against the caller's real speech bursts."""
    cut, waits, ontime = [], [], 0
    for t in completes:
        inside = next(((s, e) for s, e in regions if s < t < e), None)
        if inside:
            # It ended the turn while the caller was still inside a burst.
            cut.append((t, inside[1] - t))
            continue
        prior = [e for s, e in regions if e <= t]
        if prior:
            gap = t - max(prior)
            waits.append(gap)
            if gap <= 1.0:
                ontime += 1
    return {
        "verdicts": len(completes),
        "bursts": len(regions),
        "cut": cut,
        "cut_n": len(cut),
        "waits": waits,
        "wait_p50": statistics.median(waits) if waits else None,
        "wait_max": max(waits) if waits else None,
        "slow_n": sum(1 for w in waits if w > 1.5),
        "ontime": ontime,
    }


def fetch(run_id: int, workflow: int) -> tuple[Path, dict] | tuple[None, None]:
    """The cached wav + run log, downloading the wav only if absent."""
    cached = list(CALLER_DIR.glob(f"wf*_run{run_id}.wav"))
    log = RUNS_DIR / f"{workflow}_{run_id}.json"

    run = None
    if log.exists():
        run = json.loads(log.read_text(encoding="utf-8"))
    else:
        env = {}
        for line in (REPO / ".env").read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
        base = (env.get("VAANI_SERVER_API_URL")
                or "https://vaani.bswealthfinance.com").rstrip("/")
        req = urllib.request.Request(
            f"{base}/api/v1/workflow/{workflow}/runs/{run_id}",
            headers={"X-API-Key": env["VAANI_SERVER_API_KEY"]})
        run = json.loads(urllib.request.urlopen(req, timeout=60)
                         .read().decode("utf-8", "replace"))

    if cached:
        return cached[0], run
    url = run.get("user_recording_public_url")
    if not url:
        return None, None
    CALLER_DIR.mkdir(parents=True, exist_ok=True)
    path = CALLER_DIR / f"wf{workflow}_run{run_id}.wav"
    urllib.request.urlretrieve(url, path)
    return path, run


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", type=int)
    ap.add_argument("--workflow", type=int, default=2)
    ap.add_argument("--all", action="store_true",
                    help="every cached recording on disk")
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--model", choices=tuple(MODELS), default="shipped",
                    help="which artifact to load: shipped = what production "
                         "runs; real-gbm / real-linear = retrained on the REAL "
                         "labels (turnstops_real.jsonl)")
    ap.add_argument("--detector",
                    choices=("telugu", "smart-turn-v3", "audio-native"),
                    default="telugu",
                    help="telugu = our 250-tree forest; smart-turn-v3 = the "
                         "model bundled with pipecat, for the bake-off")
    a = ap.parse_args()

    jobs: list[tuple[Path, dict, str]] = []
    if a.run:
        wav, run = fetch(a.run, a.workflow)
        if not wav:
            print(f"no caller recording for run {a.run}")
            return 1
        jobs.append((wav, run, f"wf{a.workflow} run{a.run}"))
    elif a.all:
        for wav in sorted(CALLER_DIR.glob("*.wav"))[:a.limit]:
            stem = wav.stem                       # wf<W>_run<R>
            try:
                wf, rid = stem.split("_run")
                log = RUNS_DIR / f"{wf[2:]}_{rid}.json"
                if not log.exists():
                    continue
                jobs.append((wav, json.loads(log.read_text(encoding="utf-8")),
                             stem))
            except ValueError:
                continue
    else:
        ap.error("pass --run N or --all")

    print(f"\n{'call':22} {'bursts':>7} {'verdicts':>9} {'CUT OFF':>8} "
          f"{'wait p50':>9} {'wait max':>9} {'slow':>5}")
    print("-" * 76)
    tot_cut = tot_burst = tot_slow = 0
    all_waits: list[float] = []
    for wav, run, label in jobs:
        r = replay(wav, run, detector=a.detector, model=a.model)
        if r.get("error"):
            print(f"{label:22} {r['error']}")
            continue
        p50 = f"{r['wait_p50']:.2f}s" if r["wait_p50"] is not None else "   -  "
        mx = f"{r['wait_max']:.2f}s" if r["wait_max"] is not None else "   -  "
        flag = f"  <-- CUT OFF x{r['cutoffs']}" if r["cutoffs"] else ""
        print(f"{label:22} {r['bursts']:>7} {r['verdicts']:>9} {r['cutoffs']:>8} "
              f"{p50:>9} {mx:>9} {r['slow_n']:>5}{flag}")
        tot_cut += r["cutoffs"]
        tot_burst += r["bursts"]
        tot_slow += r["slow_n"]
        all_waits += r["waits"]

    print("-" * 76)
    print(f"{len(jobs)} calls, {tot_burst} speech bursts")
    if tot_burst:
        print(f"  CUT OFF        {tot_cut:>4}  ({tot_cut / tot_burst:.1%} of bursts)"
              f"   target < 2%")
    if all_waits:
        print(f"  wait p50       {statistics.median(all_waits):.2f}s   "
              f"p90 {statistics.quantiles(all_waits, n=10)[8]:.2f}s"
              if len(all_waits) >= 10 else
              f"  wait p50       {statistics.median(all_waits):.2f}s")
        print(f"  slow (>1.5s)   {tot_slow:>4}  — the caller sitting in silence")
    print("\n  Cut-offs and slow turns are the SAME defect from two ends. A change"
          "\n  that lowers one and raises the other has not fixed anything.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
