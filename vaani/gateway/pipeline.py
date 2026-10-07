"""Vaani gateway on Pipecat -- ONE system prompt, no nodes, no graph.

Why this file replaces the hand-written server.py
-------------------------------------------------
The old gateway carried its own duplex audio logic: an energy threshold to
guess barge-in, an echo-tail gate, a silence watchdog. On real calls that gate
threw the caller's voice away while the agent was speaking -- the agent talked
over the caller and never heard them. Real-time audio is not a place to guess.

Pipecat solves exactly that layer, and its conversation model is the one we
want anyway: a single LLM context with a system prompt. There is no node, no
edge, and therefore no edge to fall off -- which is the structural cure for the
Dograh silence bug (plan Part 2.1).

What we keep:  the brain. brain/compiler.py, the psychology layers, triage and
               guardrails are transport-independent and come across untouched.
What we drop:  every line of hand-written audio timing.

Vobiz speaks Plivo's AudioStream protocol verbatim
--------------------------------------------------
  Vobiz -> us   start / media / playedStream
  us -> Vobiz   playAudio / clearAudio / checkpoint

That is byte-for-byte Plivo, so `PlivoFrameSerializer` drives Vobiz with no
changes. Confirmed against pipecat/serializers/plivo.py.

Turn detection
--------------
Plan Finding 1 called for a fine-tuned turn-boundary classifier as the biggest
single latency win. Pipecat ships one: smart-turn v3, ONNX, on CPU, locally. It
replaces core/endpointer.py's rules baseline -- a trained model beats our
lexicon, and it costs us nothing to adopt.
"""

from __future__ import annotations

import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import PlainTextResponse
from loguru import logger

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.frames.frames import LLMRunFrame
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.serializers.plivo import PlivoFrameSerializer
from pipecat.services.cartesia.tts import CartesiaTTSService
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.services.sarvam.stt import SarvamSTTService
from pipecat.transcriptions.language import Language
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from vaani.brain.compiler import Brief, compile_prompt  # noqa: E402
from vaani.core.llm import LLMClient  # noqa: E402
from vaani.gateway.brain_processor import ReplyFilter, StateInjector  # noqa: E402

logger.remove()
logger.add(sys.stderr, level=os.environ.get("LOG_LEVEL", "INFO"))


# --- the extraction model ----------------------------------------------------
# Fills KNOWN so an answered question leaves STILL_NEED. Without it the
# checklist never shrinks and the agent re-asks question one for the whole
# call -- the defect found on 2026-09-10.
#
# Opened ONCE at boot, not per call: LLMClient pre-warms its connection with a
# models GET, and paying that handshake when the phone is already ringing would
# push the greeting past its latency budget.
EXTRACT_LLM: LLMClient | None = None


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    global EXTRACT_LLM
    try:
        EXTRACT_LLM = await LLMClient().__aenter__()
        logger.info("extraction model ready")
    except Exception as exc:  # noqa: BLE001 - never block the server from booting
        # Degrade, do not crash: no extraction means the old repeated-question
        # behaviour, which is bad. A server that will not boot is worse.
        EXTRACT_LLM = None
        logger.error(f"extraction model unavailable, KNOWN will stay empty: {exc}")
    yield
    if EXTRACT_LLM is not None:
        await EXTRACT_LLM.__aexit__(None, None, None)
        EXTRACT_LLM = None


app = FastAPI(lifespan=_lifespan)

# --- the agent, compiled once at boot ---------------------------------------
# Four layers -> one flat system prompt. This is the whole "flow". There is no
# second prompt, no per-turn template, no node.
BRIEF_PATH = os.environ.get("VAANI_BRIEF", "clients/bswealth.yaml")
BRIEF: Brief = Brief.from_yaml(BRIEF_PATH)
SYSTEM_PROMPT: str = compile_prompt(BRIEF)

CARTESIA_VOICE = os.environ.get("CARTESIA_VOICE_ID", "faf0731e-dfb9-4cfc-8119-259a79b27e12")
CARTESIA_MODEL = os.environ.get("CARTESIA_MODEL", "sonic-3")   # only model with Telugu

# Sarvam's OpenAI-compatible endpoint. Measured 247ms to first token from
# Mumbai (bench/FINDINGS.md) -- the fastest option that speaks Telugu well.
LLM_BASE_URL = os.environ.get("VAANI_LLM_BASE_URL", "https://api.sarvam.ai/v1")
LLM_MODEL = os.environ.get("VAANI_LLM_MODEL", "sarvam-105b-conversations")
LLM_API_KEY = os.environ.get("VAANI_LLM_API_KEY") or os.environ.get("SARVAM_API_KEY", "")

TELUGU = getattr(Language, "TE_IN", None) or Language.TE

# Vobiz fetches this to start the media stream.
ANSWER_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    "<Response>\n"
    '  <Stream bidirectional="true" keepCallAlive="true" '
    'contentType="audio/x-mulaw;rate=8000">{ws}</Stream>\n'
    "</Response>"
)


def _ws_url(request: Request) -> str:
    host = os.environ.get("VAANI_PUBLIC_HOST") or request.url.netloc
    return f"wss://{host}/ws"


@app.get("/health")
async def health() -> dict:
    return {"ok": True, "engine": "pipecat", "model": LLM_MODEL,
            "prompt_chars": len(SYSTEM_PROMPT), "nodes": 0}


@app.api_route("/answer", methods=["GET", "POST"])
async def answer(request: Request) -> PlainTextResponse:
    return PlainTextResponse(ANSWER_XML.format(ws=_ws_url(request)),
                             media_type="application/xml")


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()

    # Vobiz sends `start` first; the serializer needs streamId from it.
    start_msg = json.loads(await websocket.receive_text())
    start = start_msg.get("start", {}) or start_msg
    stream_id = start.get("streamId") or start.get("stream_id") or ""
    call_id = start.get("callId") or start.get("call_id") or ""
    logger.info(f"call start stream={stream_id} call={call_id}")

    serializer = PlivoFrameSerializer(
        stream_id=stream_id,
        call_id=call_id,
        # Vobiz is not Plivo: it has no Plivo REST API to hang up through, so we
        # end the call by closing the socket instead.
        params=PlivoFrameSerializer.InputParams(auto_hang_up=False),
    )

    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            add_wav_header=False,
            serializer=serializer,
        ),
    )

    stt = SarvamSTTService(
        api_key=os.environ["SARVAM_API_KEY"],
        model="saaras:v3",
        params=SarvamSTTService.InputParams(language=TELUGU),
    )

    llm = OpenAILLMService(
        api_key=LLM_API_KEY,
        base_url=LLM_BASE_URL,
        model=LLM_MODEL,
    )

    tts = CartesiaTTSService(
        api_key=os.environ["CARTESIA_API_KEY"],
        voice_id=CARTESIA_VOICE,
        model=CARTESIA_MODEL,
        params=CartesiaTTSService.InputParams(language=TELUGU),
    )

    # ONE context. One system message. Nothing else steers the conversation.
    # ONE context. One system message. Nothing else steers the conversation.
    context = LLMContext([{"role": "system", "content": SYSTEM_PROMPT}])

    # Turn-taking lives HERE in Pipecat 1.7, not on the transport. Passing
    # vad_analyzer/turn_analyzer to the transport params is silently ignored --
    # which is exactly how the first build ended up with no VAD at all and an
    # agent that never heard the caller.
    #
    # The default strategies are already what the plan asked for:
    #   start: VAD + transcription
    #   stop:  TurnAnalyzerUserTurnStopStrategy(LocalSmartTurnAnalyzerV3)
    # i.e. trained semantic endpointing instead of a fixed silence timeout.
    aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(params=VADParams(
                confidence=0.6,
                start_secs=0.12,   # react fast to the caller starting
                stop_secs=0.20,    # smart-turn decides the real boundary
                min_volume=0.4,    # phone audio is quieter than studio audio
            )),
        ),
    )

    # The brain, on the live path -- the same modules the simulator gates on.
    injector = StateInjector(BRIEF, context, SYSTEM_PROMPT, llm=EXTRACT_LLM)
    reply_filter = ReplyFilter(injector)

    pipeline = Pipeline([
        transport.input(),
        stt,
        injector,          # triage + fresh state block, before the LLM fires
        aggregator.user(),
        llm,
        reply_filter,      # strip MODE, enforce hard rules, before TTS
        tts,
        transport.output(),
        aggregator.assistant(),
    ])

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            audio_in_sample_rate=8000,
            audio_out_sample_rate=8000,

            enable_metrics=True,          # ttfb per service, logged per turn
            enable_usage_metrics=True,
        ),
    )

    # Per-turn latency DECOMPOSITION, not one opaque number.
    #
    # Learned the hard way on 2026-08-26: a single "turn took 1.45s" figure sent
    # us optimising the wrong component for hours. The breakdown immediately
    # showed the endpoint was a fixed 0.81s clock (VAD stop + smart-turn
    # stop_secs) while the LLM was 0.19s -- the opposite of what we assumed.
    #
    # `user_turn_secs` = VAD silence + STT finalisation + turn-analyzer wait.
    # It is usually the largest term and the easiest one to misattribute.
    if task.user_bot_latency_observer:

        @task.user_bot_latency_observer.event_handler("on_latency_breakdown")
        async def _on_latency_breakdown(_observer, breakdown):
            try:
                logger.info(
                    "[latency] " + " | ".join(breakdown.chronological_events())
                )
                if breakdown.user_turn_secs is not None:
                    logger.info(
                        "[latency] endpoint (user_turn_secs) = "
                        f"{breakdown.user_turn_secs:.3f}s"
                    )
            except Exception as e:  # diagnostics must never break a call
                logger.debug(f"[latency] breakdown failed: {e}")

        @task.user_bot_latency_observer.event_handler("on_latency_measured")
        async def _on_latency_measured(_observer, latency_seconds):
            logger.info(f"[latency] TURN TOTAL = {latency_seconds:.3f}s")

    @transport.event_handler("on_client_connected")
    async def _on_connected(_transport, _client):
        # Outbound call: we speak first. Kick the LLM rather than playing a
        # canned line, so the opening is in the agent's own voice and context.
        await task.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_disconnected")
    async def _on_disconnected(_transport, _client):
        logger.info(f"call end stream={stream_id}")
        await task.cancel()

    await PipelineRunner(handle_sigint=False).run(task)
