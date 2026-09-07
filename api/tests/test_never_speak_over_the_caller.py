"""A reply that begins while the caller is still speaking is never spoken.

Defect 4 of the four that got semantic turn completion reverted after run 792,
and the one that mattered most, because it is the failure the feature exists to
prevent.

Under deferral the turn is deliberately NOT stopped while the LLM is polled for
its marker. So when a stale marker arrives after the caller has resumed, the
controller correctly leaves the turn open -- and nothing cancels the generation
that came with it. No `InterruptionFrame` is raised, because from the pipeline's
point of view the bot never took the floor. The reply is pushed to TTS on top of
a caller who is mid-sentence.

The guard lives in `ReplyFilter` rather than in the controller because this is
the last processor before TTS, and the rule holds whatever produced the text:
never speak over someone who is speaking.
"""

from __future__ import annotations

import pytest

from pipecat.frames.frames import (
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection

from api.services.vaani.brain_processor import ReplyFilter
from api.services.vaani.state import CallState


class _Injector:
    def __init__(self, state):
        self.state = state


def _filter():
    st = CallState(required_fields=["location"],
                   questions={"location": "మీరు ఏ ఏరియాలో ఉంటున్నారు?"})
    rf = ReplyFilter(_Injector(st))
    rf.spoken_out = []

    async def _collect(frame, direction=FrameDirection.DOWNSTREAM):
        if isinstance(frame, LLMTextFrame):
            rf.spoken_out.append(frame.text)

    rf.push_frame = _collect
    return rf


async def _send(rf, frame):
    await rf.process_frame(frame, FrameDirection.DOWNSTREAM)


async def _reply(rf, text):
    await _send(rf, LLMFullResponseStartFrame())
    await _send(rf, LLMTextFrame(text))
    await _send(rf, LLMFullResponseEndFrame())
    return "".join(rf.spoken_out)


@pytest.mark.asyncio
async def test_a_normal_reply_is_spoken():
    """The control. Without it the test below proves nothing."""
    rf = _filter()
    assert "మీరు ఏ ఏరియాలో" in await _reply(rf, "సరే, మీరు ఏ ఏరియాలో ఉంటున్నారు?")


@pytest.mark.asyncio
async def test_a_reply_that_starts_over_the_caller_is_not_spoken():
    rf = _filter()
    await _send(rf, UserStartedSpeakingFrame())      # he is still talking
    out = await _reply(rf, "సరే, మీరు ఏ ఏరియాలో ఉంటున్నారు?")
    assert out == "", f"spoke over the caller: {out!r}"


@pytest.mark.asyncio
async def test_the_whole_reply_is_dropped_even_if_he_stops_midway():
    """Half a sentence answering the wrong thing is worse than none."""
    rf = _filter()
    await _send(rf, UserStartedSpeakingFrame())
    await _send(rf, LLMFullResponseStartFrame())
    await _send(rf, LLMTextFrame("సరే, మీరు "))
    await _send(rf, UserStoppedSpeakingFrame())      # he finishes mid-reply
    await _send(rf, LLMTextFrame("ఏ ఏరియాలో ఉంటున్నారు?"))
    await _send(rf, LLMFullResponseEndFrame())
    assert "".join(rf.spoken_out) == ""


@pytest.mark.asyncio
async def test_the_next_reply_after_he_stops_is_spoken_normally():
    """The guard must not latch. It is per-reply, not for the rest of the call."""
    rf = _filter()
    await _send(rf, UserStartedSpeakingFrame())
    assert await _reply(rf, "బ్లాక్ చేయాలి") == ""
    await _send(rf, UserStoppedSpeakingFrame())
    rf.spoken_out.clear()
    assert "ఏ ఏరియాలో" in await _reply(rf, "సరే, మీరు ఏ ఏరియాలో ఉంటున్నారు?")
