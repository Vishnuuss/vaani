"""Pretend to be Vobiz. Exercise the whole gateway without a billed call.

This is the test that should have existed before the last five live calls. It
speaks Vobiz's exact protocol at our own websocket, feeds it real Telugu audio,
and reports what came back -- so every bug except PSTN itself is caught here,
for free.

What it proves:
  1. the greeting arrives at all (playAudio frames on connect)
  2. the agent HEARS the caller -- the bug that broke the live calls
  3. barge-in works: audio sent while the agent is speaking gets a clearAudio

Usage:
    python scripts/fake_vobiz.py                 # full run
    python scripts/fake_vobiz.py --no-bargein    # skip step 3
"""

from __future__ import annotations

import argparse
import asyncio
import audioop
import base64
import json
import os
import sys
import time
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

WS = os.environ.get("FAKE_VOBIZ_WS", "ws://127.0.0.1:8090/ws")
STREAM_ID = "fakestream001"
CALL_ID = "fakecall001"
FRAME_MS = 20
FRAME_BYTES = 160          # 20ms of 8kHz mu-law


def silence(ms: int) -> bytes:
    return b"\xff" * (FRAME_BYTES * (ms // FRAME_MS))


async def telugu_mulaw(text: str) -> bytes:
    """Render a caller utterance with Cartesia, as 8k mu-law -- real speech,
    because Silero VAD and Sarvam both reject synthetic tones."""
    import httpx

    key = os.environ["CARTESIA_API_KEY"]
    body = {
        "model_id": os.environ.get("CARTESIA_MODEL", "sonic-3"),
        "transcript": text,
        "voice": {"mode": "id", "id": os.environ["CARTESIA_VOICE_ID"]},
        "language": "te",
        "output_format": {"container": "raw", "encoding": "pcm_s16le",
                          "sample_rate": 8000},
    }
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post("https://api.cartesia.ai/tts/bytes", json=body, headers={
            "X-API-Key": key, "Cartesia-Version": "2024-06-10",
        })
        r.raise_for_status()
        return audioop.lin2ulaw(r.content, 2)


async def send_audio(ws, mulaw: bytes, *, realtime: bool = True) -> None:
    """Stream at wall-clock speed -- VAD timing is meaningless otherwise."""
    for i in range(0, len(mulaw), FRAME_BYTES):
        frame = mulaw[i:i + FRAME_BYTES]
        await ws.send(json.dumps({
            "event": "media",
            "streamId": STREAM_ID,
            "media": {"payload": base64.b64encode(frame).decode()},
        }))
        if realtime:
            await asyncio.sleep(FRAME_MS / 1000)


class Recorder:
    """Counts what the gateway sent back, and when."""

    def __init__(self) -> None:
        self.play_frames = 0
        self.play_bytes = 0
        self.clears = 0
        self.first_audio_at: float | None = None
        self.last_audio_at: float | None = None
        self.other: list[str] = []

    async def run(self, ws, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            except websockets.ConnectionClosed:
                return
            try:
                msg = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            ev = msg.get("event")
            if ev == "playAudio":
                self.play_frames += 1
                payload = msg.get("media", {}).get("payload", "")
                self.play_bytes += len(base64.b64decode(payload)) if payload else 0
                now = time.perf_counter()
                self.first_audio_at = self.first_audio_at or now
                self.last_audio_at = now
            elif ev == "clearAudio":
                self.clears += 1
            elif ev:
                self.other.append(ev)

    @property
    def audio_secs(self) -> float:
        return self.play_bytes / 8000.0    # mu-law: 1 byte per sample @ 8kHz


async def wait_for_reply(rec: "Recorder", frames_before: int, *,
                         start_timeout_s: float = 25.0,
                         quiet_ms: int = 800,
                         end_timeout_s: float = 40.0) -> bool:
    """Wait for the agent to START a new utterance, then to finish it.

    The first version of this only waited for quiet, which was already true the
    moment it was called -- so it declared "no reply" 36ms before the LLM
    actually fired. Waiting for the reply to begin is the whole point.
    """
    deadline = time.perf_counter() + start_timeout_s
    while time.perf_counter() < deadline:
        if rec.play_frames > frames_before:
            break
        await asyncio.sleep(0.05)
    else:
        return False

    deadline = time.perf_counter() + end_timeout_s
    while time.perf_counter() < deadline:
        await asyncio.sleep(0.1)
        if rec.last_audio_at and (time.perf_counter() - rec.last_audio_at) * 1000 > quiet_ms:
            return True
    return True


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-bargein", action="store_true")
    ap.add_argument("--say", default="హలో, నేను మాట్లాడుతున్నాను. మా ఇల్లు మాదే.")
    args = ap.parse_args()

    print(f"connecting to {WS}")
    async with websockets.connect(WS, max_size=None) as ws:
        rec = Recorder()
        stop = asyncio.Event()
        reader = asyncio.create_task(rec.run(ws, stop))

        t_connect = time.perf_counter()
        await ws.send(json.dumps({
            "event": "start",
            "start": {"streamId": STREAM_ID, "callId": CALL_ID,
                      "mediaFormat": {"encoding": "audio/x-mulaw",
                                      "sampleRate": 8000, "channels": 1}},
        }))

        # --- 1. does the agent greet us at all? --------------------------
        feeder = asyncio.create_task(send_audio(ws, silence(8000)))
        await wait_for_reply(rec, 0)
        feeder.cancel()
        greet_frames, greet_secs = rec.play_frames, rec.audio_secs
        ttfa = (rec.first_audio_at - t_connect) * 1000 if rec.first_audio_at else -1
        print(f"\n[1] GREETING  frames={greet_frames}  audio={greet_secs:.1f}s  "
              f"time-to-first-audio={ttfa:.0f}ms")
        if greet_frames == 0:
            print("    FAIL: agent never spoke")
            stop.set(); await reader
            return 1
        print("    PASS")

        # --- 2. does it HEAR us? (the bug that broke the live calls) -----
        print(f"\n[2] SPEAKING TO IT: {args.say!r}")
        speech = await telugu_mulaw(args.say)
        before = rec.play_frames
        t_spoke = time.perf_counter()
        await send_audio(ws, speech)
        # Keep the line open with silence -- a real phone never goes to zero,
        # and hanging up during the agent's think time is what broke run #3.
        keepalive = asyncio.create_task(send_audio(ws, silence(30000)))
        got = await wait_for_reply(rec, before)
        keepalive.cancel()
        replied = rec.play_frames - before
        reply_ms = (rec.first_audio_at and (rec.last_audio_at - t_spoke) * 1000) or -1
        print(f"    reply frames={replied}  ({rec.audio_secs - greet_secs:.1f}s of audio)")
        if replied == 0:
            print("    FAIL: agent did NOT respond -- it cannot hear the caller")
            stop.set(); await reader
            return 1
        print("    PASS -- agent heard the caller and answered")

        # --- 3. barge-in: talk over it -----------------------------------
        if not args.no_bargein:
            print("\n[3] BARGE-IN: interrupting mid-sentence")
            clears_before = rec.clears
            long_q = await telugu_mulaw("ఒక్క నిమిషం, నాకు ఒక సందేహం ఉంది.")
            # Provoke a reply, then cut across it while it is still speaking.
            await send_audio(ws, await telugu_mulaw("మీ కంపెనీ గురించి కొంచెం వివరంగా చెప్పండి."))
            await send_audio(ws, silence(400))
            for _ in range(60):                      # wait until it starts talking
                await asyncio.sleep(0.05)
                if rec.last_audio_at and (time.perf_counter() - rec.last_audio_at) < 0.3:
                    break
            await send_audio(ws, long_q)
            await asyncio.sleep(1.5)
            got = rec.clears - clears_before
            print(f"    clearAudio events={got}")
            print("    PASS -- interruption honoured" if got else
                  "    FAIL -- agent talked over the caller")

        stop.set()
        await reader
        print(f"\nunhandled events from gateway: {sorted(set(rec.other)) or 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
