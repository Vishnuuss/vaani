"""Cartesia TTS client -- one warm socket per call, mu-law 8k direct.

Measured 2026-08-25 (FINDINGS §1, §3):

  warm socket, sonic-3   164ms TTFB
  cold socket per turn   560ms TTFB      <- 396ms wasted, every single turn

  ONLY `sonic-3` supports Telugu. sonic-2, sonic-turbo and sonic all reject
  language=te with "Invalid language for model."

So: connect once at call setup, hold it for the whole call, and never let the
model default drift back to sonic-2.
"""

from __future__ import annotations

import base64
import json
import os
from typing import AsyncIterator

import websockets

from .audio import CARTESIA_OUTPUT_FORMAT

WS_URL = "wss://api.cartesia.ai/tts/websocket"
DEFAULT_MODEL = "sonic-3"          # the ONLY model that speaks Telugu
DEFAULT_LANGUAGE = "te"


class CartesiaTTS:
    def __init__(self, voice_id: str | None = None, *,
                 model_id: str | None = None, language: str | None = None):
        self.voice_id = voice_id or os.environ.get("CARTESIA_VOICE_ID", "")
        self.model_id = model_id or os.environ.get("CARTESIA_MODEL_ID", DEFAULT_MODEL)
        self.language = language or os.environ.get("CARTESIA_LANGUAGE", DEFAULT_LANGUAGE)
        self.version = os.environ.get("CARTESIA_VERSION", "2024-11-13")
        self._ws = None

    async def __aenter__(self) -> "CartesiaTTS":
        key = os.environ.get("CARTESIA_API_KEY")
        if not key:
            raise RuntimeError("CARTESIA_API_KEY not set")
        if not self.voice_id:
            raise RuntimeError("CARTESIA_VOICE_ID not set")
        url = f"{WS_URL}?api_key={key}&cartesia_version={self.version}"
        self._ws = await websockets.connect(url, max_size=None)
        return self

    async def __aexit__(self, *exc) -> None:
        if self._ws is not None:
            await self._ws.close()
            self._ws = None

    async def say(self, text: str, context_id: str) -> AsyncIterator[bytes]:
        """Synthesise one utterance, yielding raw mu-law chunks as they arrive.

        Yields incrementally so the first audio reaches the caller while the
        rest is still being generated -- waiting for the full clip would throw
        away most of the benefit of a warm socket.
        """
        await self._ws.send(json.dumps({
            "model_id": self.model_id,
            "transcript": text,
            "voice": {"mode": "id", "id": self.voice_id},
            "output_format": CARTESIA_OUTPUT_FORMAT,
            "language": self.language,
            "context_id": context_id,
        }))

        while True:
            msg = json.loads(await self._ws.recv())
            kind = msg.get("type")
            if kind == "chunk" and msg.get("data"):
                yield base64.b64decode(msg["data"])
            elif kind == "done":
                return
            elif kind == "error":
                raise RuntimeError(f"cartesia: {msg.get('error')}")

    async def synthesize(self, text: str, context_id: str = "batch") -> bytes:
        """Full clip in one buffer. For pre-rendering the filler bank only --
        never on the realtime path."""
        return b"".join([chunk async for chunk in self.say(text, context_id)])
