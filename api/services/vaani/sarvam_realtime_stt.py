"""Sarvam's realtime STT, which is where the remaining latency lives.

The number this exists to remove
---------------------------------
Run 300, decomposed correctly:

    endpoint (includes the STT wait)   0.720 s   60%
    LLM first token                    0.410 s   34%
    TTS first audio                    0.061 s    5%
    TOTAL                              1.191 s

VAD stop is 0.2 s and Sarvam's first word is 0.373 s. Together that is 0.573 s
of the 0.720 s endpoint: the caller stops talking, and the pipeline sits there
waiting for text that only starts being produced once the utterance is over.

`saarika:v2.5` -- what production runs -- is batch-shaped. Its pipecat service
has no partial path at all; `InterimTranscriptionFrame` is not even imported
there, and the one `TranscriptionFrame` is pushed on the socket's utterance-end
message. So the wait is structural, not tuning.

Measured, on this project's own caller recordings replayed at wall-clock speed
(`tools/probe_sarvam_realtime.py`), 8 kHz phone audio, Telugu:

    model                     speech end -> transcript
    saarika:v2.5                        0.373 s
    saaras:v3-realtime          0.121 / 0.117 / 0.091 s

A failed experiment worth recording, because it looks like the same idea: the
plain `saaras:v3` model was tried first, since it needed only a config change.
It made things WORSE -- run 305 measured STT at 0.479 s and p50 total at
1.339 s against 1.109 s. The speed is not in the model, it is in the REALTIME
ENDPOINT, which the pipecat service does not speak. Hence this file.

What this deliberately does not do
-----------------------------------
Sarvam sends `vad.speech_start` and `vad.speech_end` on this socket, and they
are ignored on purpose. Turn-taking belongs to `TeluguTurnAnalyzer` and the
local Silero VAD -- a second opinion arriving over the network would race the
trained detector and undo the two-sided endpointing that stops this agent
talking over people. This service reports words. It does not decide turns.

Partials and speculation
-------------------------
Partials are pushed as `InterimTranscriptionFrame`, which is what preemptive
generation needs and has never once had. It is worth being honest about the
ceiling: across 10 measured utterances the final matched the last partial
exactly 2 times. The finals are re-decoded rather than promoted, and one pair
read "అవును అవును అవును" (yes yes yes) as a partial and "నో నో నో ఐ డోంట్ హావ్"
(no no no, I do not have) as the final. Speculating on that would generate a
reply to the opposite answer -- it could never be SPOKEN, because the
coordinator releases buffered tokens only on an exact match, so the cost is
wasted tokens. But a 20% hit rate is worth ~0.07 s, not the 0.33 s hoped for.

The win here is the 0.25 s, and it does not depend on speculation working.
"""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any, AsyncGenerator
from urllib.parse import urlencode

import websockets
from loguru import logger

from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    ErrorFrame,
    Frame,
    InterimTranscriptionFrame,
    StartFrame,
    TranscriptionFrame,
)
from pipecat.services.settings import STTSettings
from pipecat.services.stt_service import STTService
from pipecat.transcriptions.language import Language
from pipecat.utils.time import time_now_iso8601

MODEL = "saaras:v3-realtime"
URL = "wss://api.sarvam.ai/speech-to-text-realtime/ws"

# "fast" is Sarvam's lowest-latency stream type. The others (balanced,
# simulated) trade first-token time for stability, which is the wrong trade for
# the one number this whole file exists to move.
STREAM_TYPE = "fast"


class SarvamRealtimeSTTService(STTService):
    """Streams audio to Sarvam's realtime endpoint and reports words."""

    def __init__(
        self,
        *,
        api_key: str,
        language: str = "te-IN",
        sample_rate: int | None = None,
        stream_type: str = STREAM_TYPE,
        **kwargs,
    ):
        # The model name is read from settings, not stored on the instance --
        # that is where `_sync_model_name_to_metrics` looks, and it is what puts
        # the model into every `rtf-ttfb-metric` in the call log. Without it the
        # metric arrives unlabelled and the two STT paths are indistinguishable
        # after the fact, which is exactly the confusion that made run 305 look
        # like a saarika result.
        super().__init__(
            sample_rate=sample_rate,
            settings=STTSettings(model=MODEL, language=language or "te-IN"),
            **kwargs,
        )
        self._api_key = api_key
        self._language = language or "te-IN"
        self._stream_type = stream_type
        self._ws: Any = None
        self._receive_task: asyncio.Task | None = None
        self._user_id = ""

    def can_generate_metrics(self) -> bool:
        return True

    # --- lifecycle ---------------------------------------------------------

    async def start(self, frame: StartFrame):
        await super().start(frame)
        await self._connect()

    async def stop(self, frame: EndFrame):
        await super().stop(frame)
        await self._disconnect()

    async def cancel(self, frame: CancelFrame):
        await super().cancel(frame)
        await self._disconnect()

    async def _connect(self):
        if self._ws is not None:
            return
        query = urlencode({
            "model": MODEL,
            "language_code": self._language,
            # transcribe, not translate: the caller's Telugu must stay Telugu.
            # The translate modes return English, which would silently change
            # what every downstream parser in this package is reading.
            "mode": "transcribe",
            "stream_type": self._stream_type,
            "encoding": "linear16",
            "sample_rate": str(self.sample_rate or 8000),
        })
        try:
            self._ws = await websockets.connect(
                f"{URL}?{query}",
                additional_headers={"api-subscription-key": self._api_key},
                max_size=None,
            )
        except Exception as e:
            logger.error(f"[sarvam-realtime] connect failed: {e!r}")
            self._ws = None
            return
        self._receive_task = self.create_task(self._receive())
        logger.info(f"[sarvam-realtime] connected, {self._language}, "
                    f"{self.sample_rate} Hz, stream_type={self._stream_type}")

    async def _disconnect(self):
        if self._receive_task:
            await self.cancel_task(self._receive_task)
            self._receive_task = None
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

    # --- audio in ----------------------------------------------------------

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame | None, None]:
        """Send one chunk. Transcripts come back on the receive task."""
        if self._ws is None:
            yield None
            return
        try:
            await self._ws.send(json.dumps({
                "event": "audio_input",
                "audio": base64.b64encode(audio).decode("utf-8"),
            }))
        except Exception as e:
            # A dropped socket must not take the call down silently. Clearing it
            # lets the next chunk re-establish, and the turn analyzer keeps the
            # conversation moving on audio alone meanwhile.
            logger.warning(f"[sarvam-realtime] send failed, reconnecting: {e!r}")
            self._ws = None
            await self._connect()
            yield ErrorFrame(error=f"Sarvam realtime send failed: {e}", exception=e)
        yield None

    # --- transcripts out ---------------------------------------------------

    async def _receive(self):
        ws = self._ws
        if ws is None:
            return
        try:
            async for raw in ws:
                await self._handle(json.loads(raw))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"[sarvam-realtime] receive ended: {e!r}")

    async def _handle(self, msg: dict):
        kind = msg.get("event") or msg.get("type") or ""
        text = (msg.get("text") or "").strip()

        if kind == "error":
            # Quota exhaustion arrives here, and it is the one error that must
            # be loud: the agent goes deaf while every other part of the call
            # continues to look healthy.
            logger.error(f"[sarvam-realtime] {msg.get('code')}: {msg.get('message')}")
            await self.push_error(error_msg=f"Sarvam realtime: {msg.get('message')}")
            return

        if not text:
            # Empty partials are frequent -- the decoder flickers to nothing and
            # back while it re-scores. Pushing them would clear text the
            # aggregator is holding.
            return

        if "partial" in kind:
            await self.push_frame(InterimTranscriptionFrame(
                text, self._user_id, time_now_iso8601(),
                self._language_enum(), result=msg))
        elif "final" in kind:
            # `finalized=True` is what makes the base class report TTFB against
            # this frame rather than waiting out its timeout, so the number in
            # the call log is the real speech-end-to-text figure.
            frame = TranscriptionFrame(
                text, self._user_id, time_now_iso8601(),
                self._language_enum(), result=msg)
            frame.finalized = True
            await self.push_frame(frame)

    def _language_enum(self) -> Language | None:
        try:
            return Language(self._language)
        except ValueError:
            return None
