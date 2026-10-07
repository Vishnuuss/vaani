"""Vobiz gateway -- the server that answers the phone.

    POST /answer   Vobiz asks what to do -> we return <Stream> XML
    WS   /ws       Vobiz streams call audio here, both directions

Wire protocol (verified against docs.vobiz.ai, 2026-08-25):

    Vobiz -> us   start        callId, streamId, mediaFormat
    Vobiz -> us   media        base64 mu-law 8k
    Vobiz -> us   playedStream confirms audio REACHED THE CALLER
    us -> Vobiz   playAudio    base64 mu-law 8k
    us -> Vobiz   clearAudio   barge-in: drop everything queued
    us -> Vobiz   checkpoint   mark the end of an utterance

The three mechanisms that make the latency budget work all live here:
  1. the silence watchdog   (no dead air, ever)
  2. responding off PARTIALS (never waiting for transcript.final)
  3. speculative execution   (LLM already in flight when the caller stops)
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response

from ..brain import compiler, guardrails, triage, state as state_mod
from ..core import endpointer as ep
from ..core.audio import FRAME_BYTES, VOBIZ_CONTENT_TYPE, SAMPLE_RATE, duration_ms, energy, frames
from ..core.filler import Cover, FillerBank, SilenceWatchdog
from ..core.llm import LLMClient
from ..core.speculator import Speculator
from ..core.stt import SarvamSTT
from ..core.tts import CartesiaTTS

# uvicorn does not configure our loggers, so nothing we log would ever appear.
# Without this the first real call gave us no transcript and no timings at all.
logging.basicConfig(
    level=os.environ.get("VAANI_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)-5s %(name)s | %(message)s",
)
for _n in ("vaani", "vaani.gateway", "vaani.extractor"):
    logging.getLogger(_n).setLevel(logging.INFO)

log = logging.getLogger("vaani.gateway")
CALL_LOG_DIR = Path(os.environ.get("VAANI_CALL_LOGS", "/app/calls"))

app = FastAPI(title="Vaani Gateway")

PUBLIC_WS_URL = os.environ.get("VAANI_PUBLIC_WS_URL", "wss://example.invalid/ws")
BRIEF_PATH = os.environ.get("VAANI_BRIEF", "clients/bswealth.yaml")

# Barge-in: sustained caller energy while we are speaking.
# Raised from 0.06 after call 01fb4c93: the agent's own echo on the inbound
# track was loud enough to trigger barge-in against itself.
BARGE_ENERGY = 0.16
BARGE_MS = 200
# After we stop speaking, the tail of our own audio keeps arriving on the
# inbound leg for a moment. Feeding that to STT produced the "ఆ తర్వాత ఆ తర్వాత"
# hallucination loop -- the agent transcribing itself, forty times over.
ECHO_TAIL_MS = 250
# Below this the frame is room tone, not speech. Sending silence to Sarvam is
# what makes it hallucinate repeated words in the first place.
SILENCE_ENERGY = 0.02
# Below this duration a caller sound is a backchannel ("హా", "mm"), not an
# interruption. Stopping for those makes the agent sound nervous and broken.
BACKCHANNEL_MS = 400

# Start speaking at the first clause boundary instead of waiting for the whole
# reply. Call 1434764e showed MISS turns at 1300-1900ms when the LLM itself
# needs only ~250ms -- the rest was us buffering the full generation before
# TTS ever started, which also drove cover audio to 50% of turns.
CLAUSE_END = ("।", "?", "!", ".", ",", chr(10))
MIN_CLAUSE_CHARS = 18


# ---------------------------------------------------------------------------
# Answer URL
# ---------------------------------------------------------------------------
@app.post("/answer")
@app.get("/answer")
async def answer(request: Request) -> Response:
    """Vobiz hits this when a call connects. Hand it our websocket.

    keepCallAlive is REQUIRED -- without it a natural pause in the conversation
    can drop the call.
    """
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<Response>\n"
        f'  <Stream bidirectional="true" keepCallAlive="true" '
        f'contentType="{VOBIZ_CONTENT_TYPE};rate={SAMPLE_RATE}">'
        f"{PUBLIC_WS_URL}</Stream>\n"
        "</Response>"
    )
    log.info("answer_url hit -> streaming to %s", PUBLIC_WS_URL)
    return Response(content=xml, media_type="application/xml")


@app.get("/health")
async def health() -> dict:
    return {"ok": True, "ws": PUBLIC_WS_URL}


# ---------------------------------------------------------------------------
# Call session
# ---------------------------------------------------------------------------
@dataclass
class Turn:
    """One agent utterance, for barge-in truncation accounting."""
    checkpoint_id: str
    text: str
    played: bool = False


@dataclass
class Session:
    ws: WebSocket
    stream_id: str = ""
    call_id: str = ""
    history: list[dict] = field(default_factory=list)
    turns: list[Turn] = field(default_factory=list)
    speaking: bool = False
    barge_ms: float = 0.0
    last_spoke_at: float = 0.0
    timings: list = field(default_factory=list)
    _send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def send(self, payload: dict) -> None:
        """Vobiz requires serialized writes -- never write from two tasks."""
        async with self._send_lock:
            await self.ws.send_text(json.dumps(payload))

    async def play(self, mulaw: bytes) -> None:
        for frame in frames(mulaw, FRAME_BYTES):
            await self.send({
                "event": "playAudio",
                "streamId": self.stream_id,
                "media": {
                    "contentType": VOBIZ_CONTENT_TYPE,
                    "sampleRate": SAMPLE_RATE,
                    "payload": base64.b64encode(frame).decode("ascii"),
                },
            })

    async def checkpoint(self, name: str) -> None:
        await self.send({"event": "checkpoint", "streamId": self.stream_id,
                         "name": name})

    async def clear(self) -> None:
        """Barge-in. Drop everything queued -- the caller is talking."""
        await self.send({"event": "clearAudio", "streamId": self.stream_id})


@app.websocket("/ws")
async def media(ws: WebSocket) -> None:
    await ws.accept()
    session = Session(ws=ws)
    log.info("websocket accepted")
    try:
        await _run_call(session)
    except WebSocketDisconnect:
        log.info("caller hung up")
    except Exception:
        log.exception("call failed")
    finally:
        try:
            CALL_LOG_DIR.mkdir(parents=True, exist_ok=True)
            name = session.call_id or "unknown"
            lines = [f"call {name}", ""]
            for m in session.history:
                who = "AGENT " if m["role"] == "assistant" else "CALLER"
                lines.append(f"{who}: {m['content']}")
            if session.timings:
                t = sorted(session.timings)
                lines += ["", f"turn latency p50 {t[len(t)//2]:.0f}ms "
                              f"min {t[0]:.0f}ms max {t[-1]:.0f}ms "
                              f"over {len(t)} turns"]
            (CALL_LOG_DIR / f"{name}.txt").write_text(chr(10).join(lines), encoding="utf-8")
            log.info("call ended -- transcript saved (%d turns)", len(session.timings))
        except Exception:
            log.exception("could not save transcript")


async def _run_call(session: Session) -> None:
    # --- wait for start ---------------------------------------------------
    while not session.stream_id:
        msg = json.loads(await session.ws.receive_text())
        if msg.get("event") == "start":
            start = msg.get("start", {})
            session.stream_id = start.get("streamId", "")
            session.call_id = start.get("callId", "")
            log.info("call %s stream %s fmt=%s",
                     session.call_id, session.stream_id, start.get("mediaFormat"))

    brief = compiler.Brief.from_yaml(BRIEF_PATH)
    system_prompt = compiler.compile_prompt(brief)
    call_state = state_mod.CallState(required_fields=brief.field_names)

    bank = FillerBank(voice_id=os.environ.get("CARTESIA_VOICE_ID", "")).load()
    if not bank.ready:
        log.warning("filler bank EMPTY -- run scripts/render_fillers.py. "
                    "Dead air is possible until you do.")

    async with LLMClient() as llm, CartesiaTTS() as tts, SarvamSTT() as stt:

        async def speak_stream(deltas) -> str:
            """Stream LLM deltas into TTS at clause boundaries.

            Returns the full spoken text. The first clause reaches the caller
            while the rest is still being generated -- which is the whole point.
            """
            spoken, buf = [], ""
            async for delta in deltas:
                buf += delta
                if len(buf) >= MIN_CLAUSE_CHARS and buf.rstrip()[-1:] in CLAUSE_END:
                    chunk = buf.strip()
                    buf = ""
                    if guardrails.check(chunk).ok:
                        await speak(chunk)
                        spoken.append(chunk)
                    else:
                        await speak(guardrails.SAFE_FALLBACK)
                        spoken.append(guardrails.SAFE_FALLBACK)
                        return " ".join(spoken)
            tail = buf.strip()
            if tail:
                if not guardrails.check(tail).ok:
                    tail = guardrails.SAFE_FALLBACK
                await speak(tail)
                spoken.append(tail)
            return " ".join(spoken)

        async def speak(text: str, *, cover: bool = False) -> None:
            """Synthesise and play, tracking what actually reached the caller."""
            if not text.strip():
                return
            checkpoint_id = uuid.uuid4().hex[:8]
            session.turns.append(Turn(checkpoint_id, text))
            session.speaking = True
            try:
                async for chunk in tts.say(text, context_id=checkpoint_id):
                    if not session.speaking:      # barge-in cancelled us
                        break
                    watchdog.notify_audio_started()
                    await session.play(chunk)
                await session.checkpoint(checkpoint_id)
            finally:
                session.speaking = False
                session.last_spoke_at = time.perf_counter()

        async def on_cover(clip: bytes) -> None:
            """Watchdog fired: real audio was too slow. Never leave dead air."""
            log.info("cover audio fired (rate now %.0f%%)", watchdog.cover_rate * 100)
            session.speaking = True
            await session.play(clip)
            session.speaking = False
            session.last_spoke_at = time.perf_counter()

        watchdog = SilenceWatchdog(bank, on_cover)

        def build_messages(transcript: str) -> list[dict]:
            return compiler.build_messages(
                system_prompt, call_state.render(), session.history, transcript)

        speculator = Speculator(llm, build_messages)

        # --- greeting -----------------------------------------------------
        greeting = (f"నమస్కారం సార్, నేను {brief.agent_name}, {brief.business} నుంచి "
                    f"మాట్లాడుతున్నాను. ఒక్క రెండు నిమిషాలు మాట్లాడొచ్చా?")
        await speak(greeting)
        session.history.append({"role": "assistant", "content": greeting})

        # --- shared turn state --------------------------------------------
        pending = {"partial": "", "last_audio": time.perf_counter(), "committing": False}

        async def commit_turn() -> None:
            """The caller has finished. Respond -- and never go silent."""
            transcript = pending["partial"].strip()
            if not transcript or pending["committing"]:
                return
            pending["committing"] = True
            t_commit = time.perf_counter()
            try:
                # Zero-latency hard-stop check before we commit to a reply.
                triage.apply(call_state, transcript)
                await stt.flush()          # final arrives later, for the record
                async with watchdog.guard(Cover.THINKING):
                    spec = speculator.commit(transcript)
                    session.history.append({"role": "user", "content": transcript})
                    if spec is not None and spec.output:
                        # HIT: already generated, speak it straight away.
                        reply = spec.output
                        if not guardrails.check(reply).ok:
                            reply = guardrails.SAFE_FALLBACK
                        await speak(reply)
                    else:
                        # MISS: stream into TTS clause by clause so the first
                        # words leave while the rest is still generating.
                        reply = await speak_stream(
                            llm.stream(build_messages(transcript)))
                    session.history.append({"role": "assistant", "content": reply})
                call_state.advance()
                latency_ms = (time.perf_counter() - t_commit) * 1000
                log.info("TURN %d | %.0fms | spec=%s hit_rate=%.0f%% cover=%.0f%%",
                         call_state.turn, latency_ms,
                         "HIT" if spec is not None else "MISS",
                         speculator.hit_rate * 100, watchdog.cover_rate * 100)
                log.info("  CALLER: %s", transcript)
                log.info("  AGENT : %s", reply)
                session.timings.append(latency_ms)
            finally:
                pending["partial"] = ""
                pending["committing"] = False

        async def stt_loop() -> None:
            async for tr in stt.events():
                if tr.is_final:
                    # Arrives ~438ms late. Correct the record; never gate on it.
                    if session.history and session.history[-1]["role"] == "user":
                        session.history[-1]["content"] = tr.text
                    continue
                pending["partial"] = tr.text
                pending["last_audio"] = time.perf_counter()
                speculator.on_partial(tr.text)

        async def endpoint_loop() -> None:
            """Our own endpointer -- this is where the 500ms VAD tax is deleted."""
            while True:
                await asyncio.sleep(0.02)
                partial = pending["partial"]
                if not partial or pending["committing"] or session.speaking:
                    continue
                silence_ms = (time.perf_counter() - pending["last_audio"]) * 1000
                if silence_ms >= ep.wait_for(partial):
                    await commit_turn()

        async def recv_loop() -> None:
            while True:
                msg = json.loads(await session.ws.receive_text())
                event = msg.get("event")

                if event == "media":
                    audio = base64.b64decode(msg.get("media", {}).get("payload")
                                             or msg.get("payload") or "")
                    if not audio:
                        continue

                    # ECHO GATE. While we are speaking -- and for a short tail
                    # afterwards -- the inbound leg is mostly our own voice
                    # coming back. Forwarding that to STT makes the agent
                    # transcribe itself and answer itself. Only genuinely loud
                    # audio (a real interruption) gets through.
                    level = energy(audio)
                    loud = level > BARGE_ENERGY
                    since_spoke = (time.perf_counter() - session.last_spoke_at) * 1000
                    gated = session.speaking or since_spoke < ECHO_TAIL_MS
                    if (not gated or loud) and level > SILENCE_ENERGY:
                        await stt.send_audio(audio)

                    # Barge-in: sustained energy while we are speaking.
                    if session.speaking and loud:
                        session.barge_ms += duration_ms(audio)
                        if session.barge_ms >= BARGE_MS:
                            log.info("barge-in -> clearing queued audio")
                            session.speaking = False
                            speculator.cancel_all()
                            await session.clear()
                            session.barge_ms = 0.0
                    elif not session.speaking:
                        session.barge_ms = 0.0

                elif event == "playedStream":
                    # Confirms audio REACHED THE CALLER. This is what lets us
                    # truncate history to what was actually heard after a
                    # barge-in, instead of estimating from word timings.
                    name = msg.get("name") or msg.get("checkpoint")
                    for turn in session.turns:
                        if turn.checkpoint_id == name:
                            turn.played = True

                elif event in ("stop", "close"):
                    return

        await asyncio.gather(recv_loop(), stt_loop(), endpoint_loop())
