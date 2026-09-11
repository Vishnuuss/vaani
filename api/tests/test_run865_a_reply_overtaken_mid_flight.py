"""Run 865: a reply that was still correct when it started, and was not by the
time it was spoken.

    BOT : మీరు ఇంకా ఇక్కడ                      <- one reply
          సరే, మీది సొంత ఇల్లా, అపార్ట్‌మెంటా...  <- and another, same breath

`test_never_speak_over_the_caller` covers the reply that BEGINS on top of him.
This is the other half, and it is the half that was missing: `_stale` is a
snapshot taken once, at `LLMFullResponseStartFrame`, of whether he happened to
be speaking at that instant. A generation that starts in silence and is
overtaken while it streams is never marked, so every later chunk passes the
gate.

Two ways the overtaken text still reaches him, both closed here:

  1. Chunks that arrive after he starts speaking. `_gate` consults `_stale`,
     which is still False.
  2. The text held back by the repeat check. That buffer is flushed directly at
     `LLMFullResponseEndFrame` -- `await self.push_frame(LLMTextFrame(held))` --
     without passing through `_gate` at all, so it is spoken even when the
     generation has been marked stale.

What is deliberately NOT done: nothing already emitted is retracted, and a reply
that has already begun speaking is allowed to FINISH. That rule is older than
this bug and was paid for -- cutting a reply off mid-sentence is what made
callers hear "అర్థమైంది బిల్లు?" (run 783's truncation). Half a sentence is
worse than a late one. So the guard only applies while nothing has been spoken
yet, which is exactly the window the 24-character holdback creates.
"""

from __future__ import annotations

import pytest

from pipecat.frames.frames import (
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    UserStartedSpeakingFrame,
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


@pytest.mark.asyncio
async def test_a_normal_reply_is_still_spoken():
    """The control. Without it the tests below prove nothing."""
    rf = _filter()
    await _send(rf, LLMFullResponseStartFrame())
    await _send(rf, LLMTextFrame("సరే, మీరు ఏ ఏరియాలో ఉంటున్నారు?"))
    await _send(rf, LLMFullResponseEndFrame())
    assert "మీరు ఏ ఏరియాలో" in "".join(rf.spoken_out)


@pytest.mark.asyncio
async def test_he_starts_speaking_mid_generation_and_hears_nothing_of_it():
    rf = _filter()
    await _send(rf, LLMFullResponseStartFrame())   # he is silent: not stale
    await _send(rf, UserStartedSpeakingFrame())    # he takes the floor
    await _send(rf, LLMTextFrame("సరే, మీది సొంత ఇల్లా, అపార్ట్‌మెంటా?"))
    await _send(rf, LLMFullResponseEndFrame())
    assert "".join(rf.spoken_out) == "", (
        "he was answering something else by the time this arrived. Run 865 "
        "spoke it anyway, on top of the reply already in the air")


@pytest.mark.asyncio
async def test_the_held_back_text_is_not_flushed_over_him_either():
    """The buffer bypasses `_gate` entirely -- it is pushed directly."""
    rf = _filter()
    await _send(rf, LLMFullResponseStartFrame())
    # Under the 24-character holdback this is still buffered, not spoken.
    await _send(rf, LLMTextFrame("సరే"))
    assert "".join(rf.spoken_out) == "", "precondition: still held back"
    await _send(rf, UserStartedSpeakingFrame())
    await _send(rf, LLMFullResponseEndFrame())
    assert "".join(rf.spoken_out) == "", (
        "the end-frame flush pushes held text straight to TTS without asking "
        "whether the caller has taken the floor since")


@pytest.mark.asyncio
async def test_a_reply_already_being_spoken_is_allowed_to_finish():
    """Never retract audio. Run 783's truncation is worse than a late reply."""
    rf = _filter()
    await _send(rf, LLMFullResponseStartFrame())
    # No "?" in this chunk: `ReplySanitizer` ends a turn at the first question
    # mark, and a chunk after one is dropped for that reason and not this one.
    await _send(rf, LLMTextFrame("మేము వెరిఫైడ్ వెండర్లను కనెక్ట్ చేస్తాం అండి."))
    assert "".join(rf.spoken_out), "precondition: audio is already out"
    already = "".join(rf.spoken_out)

    await _send(rf, UserStartedSpeakingFrame())
    await _send(rf, LLMTextFrame(" ఉచిత సర్వే కూడా ఉంటుంది."))
    await _send(rf, LLMFullResponseEndFrame())
    assert "".join(rf.spoken_out).startswith(already), (
        "nothing spoken may be unspoken")
    assert "ఉచిత సర్వే" in "".join(rf.spoken_out), (
        "a sentence that has started must finish -- cutting it off here is "
        "the truncation bug, not a fix for it")
