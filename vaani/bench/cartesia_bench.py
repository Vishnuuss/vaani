"""Cartesia TTS benchmark -- warm socket vs cold socket, mu-law 8k direct.

Two things the plan asserts and this measures:

  1. A warm, voice-preloaded websocket beats opening a connection per utterance.
     If the gap is large, "never open a socket per utterance" (plan 1.4) is
     proven rather than assumed.
  2. Requesting pcm_mulaw @ 8000 directly means the TTS bytes go to the PSTN
     wire with no resampling at all. We request exactly that format here so the
     number reflects the real pipeline, not a 44.1kHz lab setting.
"""

from __future__ import annotations

import asyncio
import json
import os
import time

from .report import Section

WS_URL = "wss://api.cartesia.ai/tts/websocket"

BUDGET_WARM_TTFB_MS = 100.0   # plan's budget table
BUDGET_COLD_TTFB_MS = 400.0   # informational -- we do not use the cold path

# Short, realistic first clause. TTFB is what we measure, so the utterance only
# needs to be long enough to produce several chunks.
UTTERANCES = [
    "నమస్కారం సార్, నేను ఆక్మే సోలార్ నుంచి మాట్లాడుతున్నాను.",
    "మీ ఇల్లు మీదేనా సార్?",
    "సరే సార్, అర్థమైంది.",
    "నెలకి మూడు వేల బిల్లు అంటే మంచి సేవింగ్ వస్తుంది.",
]

# The exact wire format the PSTN leg wants. Do not change without re-reading
# plan section 1.4 -- any other value reintroduces a resample.
OUTPUT_FORMAT = {"container": "raw", "encoding": "pcm_mulaw", "sample_rate": 8000}


def _config() -> dict:
    return {
        "api_key": os.environ.get("CARTESIA_API_KEY", ""),
        "version": os.environ.get("CARTESIA_VERSION", "2024-11-13"),
        "model_id": os.environ.get("CARTESIA_MODEL_ID", "sonic-3"),
        "voice_id": os.environ.get("CARTESIA_VOICE_ID", ""),
    }


def _payload(cfg: dict, transcript: str, context_id: str) -> str:
    return json.dumps(
        {
            "model_id": cfg["model_id"],
            "transcript": transcript,
            "voice": {"mode": "id", "id": cfg["voice_id"]},
            "output_format": OUTPUT_FORMAT,
            "language": os.environ.get("CARTESIA_LANGUAGE", "te"),
            "context_id": context_id,
        }
    )


async def _first_audio_ms(ws, cfg: dict, transcript: str, context_id: str) -> float:
    """Send one utterance on an open socket, time until the first audio chunk."""
    start = time.perf_counter()
    await ws.send(_payload(cfg, transcript, context_id))
    while True:
        raw = await asyncio.wait_for(ws.recv(), timeout=20.0)
        msg = json.loads(raw)
        kind = msg.get("type")
        if kind == "chunk" and msg.get("data"):
            ttfb = (time.perf_counter() - start) * 1000.0
            # Drain the rest of this context so it cannot bleed into the next
            # measurement on a reused socket.
            while True:
                try:
                    tail = json.loads(await asyncio.wait_for(ws.recv(), timeout=20.0))
                except asyncio.TimeoutError:
                    break
                if tail.get("type") in ("done", "error"):
                    break
            return ttfb
        if kind == "error":
            raise RuntimeError(msg.get("error", "cartesia error"))
        if kind == "done":
            raise RuntimeError("stream finished with no audio chunk")


async def _run_async(sec: Section, samples: int) -> None:
    try:
        import websockets
    except ImportError:
        sec.error = "pip install websockets"
        return

    cfg = _config()
    if not cfg["api_key"]:
        sec.error = "CARTESIA_API_KEY not set. See vaani/bench/README.md step 1."
        return
    if not cfg["voice_id"]:
        sec.error = (
            "CARTESIA_VOICE_ID not set. Pick the Telugu/Hindi voice you will ship "
            "and put its id in .env -- TTFB is voice-dependent, so benchmarking a "
            "different voice would give a number you cannot reproduce."
        )
        return

    url = f"{WS_URL}?api_key={cfg['api_key']}&cartesia_version={cfg['version']}"

    m_warm = sec.metric(
        f"warm socket TTFB  ({cfg['model_id']}, mulaw 8k)  <<<",
        budget=BUDGET_WARM_TTFB_MS,
        note="this is the production path",
    )
    m_cold = sec.metric(
        "cold socket TTFB  (connect per utterance)",
        budget=BUDGET_COLD_TTFB_MS,
        note="anti-pattern, measured to quantify the cost",
    )

    # --- warm: one socket, many utterances -------------------------------
    try:
        async with websockets.connect(url, max_size=None) as ws:
            for i in range(samples):
                text = UTTERANCES[i % len(UTTERANCES)]
                try:
                    m_warm.add(await _first_audio_ms(ws, cfg, text, f"warm-{i}"))
                except Exception as exc:  # noqa: BLE001
                    if not m_warm.note.startswith("ERR"):
                        m_warm.note = f"ERR {type(exc).__name__}: {str(exc)[:80]}"
                    break
                await asyncio.sleep(0.1)
    except Exception as exc:  # noqa: BLE001
        m_warm.note = f"connect failed: {type(exc).__name__}: {str(exc)[:100]}"

    # --- cold: reconnect every time (fewer samples, it is only a contrast) --
    for i in range(max(3, samples // 3)):
        text = UTTERANCES[i % len(UTTERANCES)]
        start = time.perf_counter()
        try:
            async with websockets.connect(url, max_size=None) as ws:
                await ws.send(_payload(cfg, text, f"cold-{i}"))
                while True:
                    msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=20.0))
                    if msg.get("type") == "chunk" and msg.get("data"):
                        m_cold.add((time.perf_counter() - start) * 1000.0)
                        break
                    if msg.get("type") in ("error", "done"):
                        break
        except Exception as exc:  # noqa: BLE001
            if not m_cold.note.startswith("ERR"):
                m_cold.note = f"ERR {type(exc).__name__}: {str(exc)[:80]}"
            break
        await asyncio.sleep(0.1)

    # The whole point of the comparison, stated in one line.
    if m_warm.samples and m_cold.samples:
        from .report import Metric, pct

        saving = pct(m_cold.samples, 50) - pct(m_warm.samples, 50)
        sec.metrics.append(
            Metric(
                name="=> VERDICT",
                note=f"keeping the socket warm saves {saving:.0f}ms per turn",
            )
        )


def run(samples: int = 15) -> Section:
    sec = Section(title="CARTESIA TTS -- time to first audio byte")
    asyncio.run(_run_async(sec, samples))
    return sec
