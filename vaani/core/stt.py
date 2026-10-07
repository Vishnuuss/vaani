"""Sarvam realtime STT client (saaras:v3-realtime).

Two things learned against the live API on 2026-08-25 (see FINDINGS §4, §5):

1. `endpointing=manual` REQUIRES an explicit `speech_start` event. Without it
   the socket accepts audio forever and returns nothing -- no error, no warning,
   just silence. We use manual mode because our own endpointer decides the turn
   boundary; that deletes Sarvam's built-in 500ms silence wait.

2. We respond off PARTIALS, never waiting for `transcript.final` (measured
   438ms p50 after flush -- far too slow to sit on the critical path). The final
   arrives later and is used to correct the record for extraction and logging.

3. Partials are NOT monotonic -- they revise backwards as the decoder
   reconsiders. Consumers must handle a partial that is shorter than the last.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
from dataclasses import dataclass, field
from typing import AsyncIterator

import websockets

from .audio import SAMPLE_RATE, SARVAM_ENCODING

WS_URL = "wss://api.sarvam.ai/speech-to-text-realtime/ws"


def is_hallucination(text: str, *, max_repeat: int = 3) -> bool:
    """True if the transcript is a degenerate repeat loop.

    Whisper-family models hallucinate on near-silence by emitting the same
    token over and over. Live call 1434764e produced "పన్నెండు" (twelve) 36
    times running -- a word nobody said. Feeding that to the agent makes it
    answer noise.
    """
    toks = (text or "").split()
    if len(toks) < max_repeat + 1:
        return False
    run = 1
    for a, b in zip(toks, toks[1:]):
        run = run + 1 if a == b else 1
        if run > max_repeat:
            return True
    # Also catch A B A B A B alternation, another common degenerate mode.
    if len(toks) >= 8 and len(set(toks)) <= 2:
        return True
    return False


@dataclass
class Transcript:
    text: str
    is_final: bool
    utterance_idx: int = 0
    raw: dict = field(default_factory=dict)


class SarvamSTT:
    """One websocket per call. Feed audio in, read Transcript events out."""

    def __init__(self, language: str | None = None, *, manual_endpointing: bool = True):
        self.language = language or os.environ.get("SARVAM_LANGUAGE", "te-IN")
        self.manual = manual_endpointing
        self._ws = None
        self._speech_open = False

    def _url(self) -> str:
        params = {
            "language_code": self.language,
            "model": "saaras:v3-realtime",
            "stream_type": "fast",              # low-latency tier
            "encoding": SARVAM_ENCODING,        # mulaw -- no transcode
            "sample_rate": str(SAMPLE_RATE),
            "endpointing": "manual" if self.manual else "vad",
        }
        return WS_URL + "?" + "&".join(f"{k}={v}" for k, v in params.items())

    async def __aenter__(self) -> "SarvamSTT":
        key = os.environ.get("SARVAM_API_KEY")
        if not key:
            raise RuntimeError("SARVAM_API_KEY not set")
        self._ws = await websockets.connect(
            self._url(),
            additional_headers={"API-SUBSCRIPTION-KEY": key},
            max_size=None,
        )
        return self

    async def __aexit__(self, *exc) -> None:
        if self._ws is not None:
            try:
                await self._ws.send(json.dumps({"event": "end"}))
            except Exception:  # noqa: BLE001
                pass
            await self._ws.close()
            self._ws = None

    async def start_speech(self) -> None:
        """MUST be called before audio in manual mode, or nothing comes back."""
        if self.manual and not self._speech_open:
            await self._ws.send(json.dumps({"event": "speech_start"}))
            self._speech_open = True

    async def send_audio(self, mulaw: bytes) -> None:
        if self._speech_open is False and self.manual:
            await self.start_speech()
        await self._ws.send(json.dumps({
            "event": "audio_input",
            "audio": base64.b64encode(mulaw).decode("ascii"),
        }))

    async def flush(self) -> None:
        """Commit the current utterance. Our endpointer decides when."""
        if not self.manual:
            return
        await self._ws.send(json.dumps({"event": "speech_end"}))
        await self._ws.send(json.dumps({"event": "flush"}))
        self._speech_open = False

    async def events(self) -> AsyncIterator[Transcript]:
        async for raw in self._ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            event = msg.get("event")
            text = msg.get("text", "")
            if event in ("transcript.partial", "transcript.final"):
                if is_hallucination(text):
                    continue          # silence hallucination -- never surface it
                yield Transcript(text, event == "transcript.final",
                                 msg.get("utterance_idx", 0), msg)
