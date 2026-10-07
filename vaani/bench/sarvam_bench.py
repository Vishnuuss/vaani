"""Sarvam realtime STT benchmark -- partial cadence and finalisation cost.

Spec confirmed from docs.sarvam.ai (saaras:v3-realtime), and it changes two
things in our favour versus the original plan:

  * `encoding=mulaw` + `sample_rate=8000` are supported natively, so there is
    NO resample on the inbound path either. mu-law 8k runs end to end, both
    directions, untouched.
  * `endpointing=manual` exists. That means OUR semantic endpointer decides the
    turn boundary and sends `flush`, instead of us paying Sarvam's built-in VAD
    `silence_duration_ms` (default 500ms). That 500ms is exactly the fixed
    silence tax Finding 1 is about, and manual mode deletes it.

This benchmark measures the two numbers that matter for speculation:

  t_first_partial   -- how soon we get text to speculate on
  partial_interval  -- how often partials update (speculation re-fire cadence)
  t_final_manual    -- flush -> final transcript      (the production path)
  t_final_vad       -- silence -> final transcript    (what manual mode saves)
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from pathlib import Path

from .report import Section

WS_URL = "wss://api.sarvam.ai/speech-to-text-realtime/ws"

BUDGET_FINAL_MS = 60.0        # plan's budget for STT finalisation
BUDGET_FIRST_PARTIAL_MS = 400.0

# mu-law 8kHz mono: 8000 bytes/sec. 20ms of audio = 160 bytes.
SAMPLE_RATE = 8000
FRAME_MS = 20
FRAME_BYTES = SAMPLE_RATE * FRAME_MS // 1000

CACHED_AUDIO = Path(__file__).parent / "_fixture_te_mulaw8k.raw"

TEST_UTTERANCE = "అవును సార్, నా ఇల్లు నాదే. కరెంట్ బిల్లు నెలకి మూడు వేలు వస్తుంది."


# --------------------------------------------------------------------------
# Test audio: synthesise once with Cartesia, then cache. Real speech in the
# exact wire format beats a synthetic tone, which the VAD would reject anyway.
# --------------------------------------------------------------------------
async def _ensure_fixture() -> bytes | None:
    if CACHED_AUDIO.exists():
        return CACHED_AUDIO.read_bytes()

    api_key = os.environ.get("CARTESIA_API_KEY")
    voice_id = os.environ.get("CARTESIA_VOICE_ID")
    if not (api_key and voice_id):
        return None

    try:
        import websockets
    except ImportError:
        return None

    version = os.environ.get("CARTESIA_VERSION", "2024-11-13")
    url = f"wss://api.cartesia.ai/tts/websocket?api_key={api_key}&cartesia_version={version}"
    chunks: list[bytes] = []
    try:
        async with websockets.connect(url, max_size=None) as ws:
            await ws.send(json.dumps({
                "model_id": os.environ.get("CARTESIA_MODEL_ID", "sonic-3"),
                "transcript": TEST_UTTERANCE,
                "voice": {"mode": "id", "id": voice_id},
                "output_format": {"container": "raw", "encoding": "pcm_mulaw",
                                  "sample_rate": SAMPLE_RATE},
                "language": os.environ.get("CARTESIA_LANGUAGE", "te"),
                "context_id": "fixture",
            }))
            while True:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=30.0))
                if msg.get("type") == "chunk" and msg.get("data"):
                    chunks.append(base64.b64decode(msg["data"]))
                elif msg.get("type") in ("done", "error"):
                    break
    except Exception:  # noqa: BLE001 - fixture generation is best-effort
        return None

    if not chunks:
        return None
    audio = b"".join(chunks)
    CACHED_AUDIO.write_bytes(audio)
    return audio


async def _one_pass(audio: bytes, key: str, manual: bool) -> dict | None:
    """Stream the fixture in realtime, record partial/final timings."""
    try:
        import websockets
    except ImportError:
        return None

    params = {
        "language_code": os.environ.get("SARVAM_LANGUAGE", "te-IN"),
        "model": "saaras:v3-realtime",
        "stream_type": "fast",           # we want the low-latency tier
        "encoding": "mulaw",             # no transcode
        "sample_rate": str(SAMPLE_RATE),
        "endpointing": "manual" if manual else "vad",
    }
    if not manual:
        # Leave Sarvam's defaults alone so we measure the real built-in cost.
        params["silence_duration_ms"] = "500"

    url = WS_URL + "?" + "&".join(f"{k}={v}" for k, v in params.items())
    headers = {"API-SUBSCRIPTION-KEY": key}

    result: dict = {"first_partial": None, "partials": [], "final": None}

    async with websockets.connect(url, additional_headers=headers, max_size=None) as ws:
        audio_start = time.perf_counter()
        done = asyncio.Event()

        async def receive() -> None:
            while not done.is_set():
                try:
                    msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=15.0))
                except Exception as exc:  # noqa: BLE001
                    result.setdefault("rx_error", f"{type(exc).__name__}: {exc}"[:100])
                    return
                event = msg.get("event")
                now = (time.perf_counter() - audio_start) * 1000.0
                if event == "transcript.partial":
                    if result["first_partial"] is None:
                        result["first_partial"] = now
                    result["partials"].append(now)
                elif event == "transcript.final":
                    result["final"] = now
                    result["final_abs"] = time.perf_counter()
                    done.set()
                    return

        rx = asyncio.create_task(receive())

        if manual:
            # REQUIRED. Without this, manual mode silently accepts audio and
            # never emits a transcript -- it does not error, it just goes quiet.
            await ws.send(json.dumps({"event": "speech_start"}))

        # Stream at true realtime pace. Bursting the whole file would give a
        # fake partial cadence that we could never see on a live call.
        for off in range(0, len(audio), FRAME_BYTES):
            frame = audio[off:off + FRAME_BYTES]
            await ws.send(json.dumps({
                "event": "audio_input",
                "audio": base64.b64encode(frame).decode("ascii"),
            }))
            await asyncio.sleep(FRAME_MS / 1000.0)

        audio_end = time.perf_counter()
        if manual:
            # This is the production path: our endpointer decides, we flush.
            await ws.send(json.dumps({"event": "speech_end"}))
            await ws.send(json.dumps({"event": "flush"}))

        try:
            await asyncio.wait_for(rx, timeout=12.0)
        except asyncio.TimeoutError:
            done.set()

        if result.get("final_abs"):
            # The number we actually care about: from the END of speech to the
            # final transcript. Not from the start of the utterance.
            result["final_after_audio"] = (result["final_abs"] - audio_end) * 1000.0
    return result


async def _run_async(sec: Section, samples: int) -> None:
    key = os.environ.get("SARVAM_API_KEY")
    if not key:
        sec.error = "SARVAM_API_KEY not set. See vaani/bench/README.md step 1."
        return

    audio = await _ensure_fixture()
    if not audio:
        sec.error = (
            "No test audio. Set CARTESIA_API_KEY + CARTESIA_VOICE_ID so the fixture "
            f"can be synthesised, or drop raw mu-law 8k audio at {CACHED_AUDIO.name}."
        )
        return

    dur_s = len(audio) / SAMPLE_RATE
    m_first = sec.metric("t_first_partial (from speech start)",
                         budget=BUDGET_FIRST_PARTIAL_MS,
                         note=f"fixture is {dur_s:.1f}s of speech")
    m_gap = sec.metric("partial update interval",
                       note="speculation re-fire cadence")
    m_manual = sec.metric("t_final after flush  (endpointing=manual)  <<<",
                          budget=BUDGET_FINAL_MS,
                          note="the production path")
    m_vad = sec.metric("t_final after silence (endpointing=vad, 500ms)",
                       note="what manual mode saves us")

    for manual, metric in ((True, m_manual), (False, m_vad)):
        n = samples if manual else max(3, samples // 3)
        for _ in range(n):
            try:
                r = await _one_pass(audio, key, manual=manual)
            except Exception as exc:  # noqa: BLE001
                if not metric.note.startswith("ERR"):
                    metric.note = f"ERR {type(exc).__name__}: {str(exc)[:90]}"
                break
            if not r:
                metric.note = "pip install websockets"
                break
            if r.get("final_after_audio") is not None:
                metric.add(r["final_after_audio"])
            if manual:
                if r["first_partial"] is not None:
                    m_first.add(r["first_partial"])
                gaps = [b - a for a, b in zip(r["partials"], r["partials"][1:])]
                for g in gaps:
                    m_gap.add(g)
            await asyncio.sleep(0.2)

    if m_manual.samples and m_vad.samples:
        from .report import Metric, pct

        saved = pct(m_vad.samples, 50) - pct(m_manual.samples, 50)
        sec.metrics.append(Metric(
            name="=> VERDICT",
            note=f"manual endpointing saves {saved:.0f}ms/turn vs Sarvam's built-in VAD",
        ))


def run(samples: int = 10) -> Section:
    sec = Section(title="SARVAM STT -- partial cadence and finalisation (saaras:v3-realtime)")
    asyncio.run(_run_async(sec, samples))
    return sec
