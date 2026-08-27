"""Vaani decides the call is over; something must actually hang up.

Three real calls (server runs 1, 3, 4) all ended `user_hangup`. Run 3 turn 6 the
agent said goodbye -- "ధన్యవాదాలు, మంచి రోజు!" -- and the line stayed open until
Vishnu hung up. Zero function-call events in any log.

Root cause: the agent has TWO ways to end a call and neither completes.

  1. Dograh's `end_call` tool. Correctly created, attached, active, registered.
     But Vaani's compiled prompt installs a MODE protocol that trains the model
     to signal the end as TEXT ("MODE: END"), so it never calls the tool.

  2. Vaani's `MODE: END`. ReplyFilter parses it and sets `state.must_end`.
     Grep for every reader of that flag: guardrails.must_close() and
     state.render(). Both only change what the PROMPT says next turn.
     Nothing calls end_call_with_reason. Nothing hangs up.

EndCallBridge closes the gap: on MODE: END it waits for the goodbye to finish
playing, then ends the call through Dograh's own disposal path.
"""

import pytest

from pipecat.frames.frames import BotStoppedSpeakingFrame, TextFrame
from pipecat.processors.frame_processor import FrameDirection

from api.services.vaani.end_call_bridge import EndCallBridge


class _State:
    def __init__(self, must_end=False):
        self.must_end = must_end


class _Engine:
    def __init__(self):
        self.calls = []

    async def end_call_with_reason(self, reason, abort_immediately=False):
        self.calls.append(reason)


def _bridge(state, engine):
    b = EndCallBridge(state=state, engine=engine)

    async def _sink(frame, direction):
        pass

    b.push_frame = _sink
    return b


@pytest.mark.asyncio
async def test_hangs_up_after_the_goodbye_finishes_playing():
    engine = _Engine()
    b = _bridge(_State(must_end=True), engine)

    await b.process_frame(BotStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)

    assert engine.calls == ["end_call"], (
        "MODE: END set must_end and nothing hung up -- this is why every call "
        "recorded user_hangup"
    )


@pytest.mark.asyncio
async def test_does_not_hang_up_while_the_call_is_live():
    engine = _Engine()
    b = _bridge(_State(must_end=False), engine)
    await b.process_frame(BotStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    assert engine.calls == [], "ended a call that was still going"


@pytest.mark.asyncio
async def test_never_hangs_up_mid_sentence():
    """The goodbye must be heard. Only BotStoppedSpeaking may trigger it."""
    engine = _Engine()
    b = _bridge(_State(must_end=True), engine)
    await b.process_frame(TextFrame("ధన్యవాదాలు"), FrameDirection.DOWNSTREAM)
    assert engine.calls == [], "cut the caller off before the goodbye finished"


@pytest.mark.asyncio
async def test_hangs_up_exactly_once():
    """The transport emits BotStoppedSpeaking both ways; disposal is not idempotent-safe to spam."""
    engine = _Engine()
    b = _bridge(_State(must_end=True), engine)
    await b.process_frame(BotStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await b.process_frame(BotStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    assert engine.calls == ["end_call"]


@pytest.mark.asyncio
async def test_a_failing_engine_never_breaks_the_call():
    class Boom:
        async def end_call_with_reason(self, reason, abort_immediately=False):
            raise RuntimeError("boom")

    b = _bridge(_State(must_end=True), Boom())
    await b.process_frame(BotStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)  # must not raise
